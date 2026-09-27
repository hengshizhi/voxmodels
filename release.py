"""发布脚本：把本库的一个条目打成 zip 并发到 GitHub Release（一次发布 = 一个模型或音色）。

本仓库**只存索引**，模型本体（权重 / 音色向量）不进 git —— 它们由本脚本打包上传到 Release。
配套的**索引同步**脚本在 MeanVC2 主仓：`finetune/voxmodels.py`（本机资产 → 库里的 `<Name>.json`）。

用法（在本仓库根目录跑）：
    python release.py --list                                   # 看有哪些、发布没发布
    python release.py --list --kind timbre --match '^AISHELL'   # 预览某批
    python release.py --kind timbre --name 冷冷v2                # 只打包，不发布
    python release.py --kind timbre --name 冷冷v2 --publish
    python release.py --kind timbre --all --match '^AISHELL' --exclude '@pp' --publish

⚠️ 五个刻意的设计（改之前先读）：

1. **zip 内的索引叫 `cf.json`**，不叫 `<Name>.json`。解压后 `<Name>/cf.json` 正是应用认的
   形态（`app/models.py`、`registry.read_manifest` 都硬编码 `cf.json`），下载方**改都不用改**。

2. **zip 顶层就是 `<Name>/`**，与 `finetune/exports/<Name>/`、音色根一一对应 —— 解压即用。

3. **发布成功才回填链接**。`download` / `release` 由本脚本在 `gh release create` 之后写入索引；
   失败不回填（宁可为空，也不要一个指向不存在资产的直链）。

4. **回填完必须再提交一次**（`git commit` + `push`）。踩过这个坑：先推索引、后发布，
   结果 GitHub 上的索引里两个字段全是空的 —— 回填只改了工作区。

5. **已有 release 不覆盖**（除非 `--force`）：当成"已发布"直接回填链接。这比重新上传安全，
   也让本脚本可重复运行 —— 弱网下失败的条目**重跑一遍**即可，不会重复上传或撞名。

本脚本**自包含**：只读本仓库的索引，不依赖 MeanVC2 的代码。本体文件靠搜索 MeanVC2 的
资产目录定位（`MEANVC2_DIR` 环境变量或 `--meanvc2` 可指定），找不到就用 `--src` 直接给目录。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

LIB_ROOT = os.path.dirname(os.path.abspath(__file__))
CATEGORIES = ("base", "derivation", "timbre")
INDEX_EXT = ".json"
PACK_INDEX_NAME = "cf.json"                    # zip 内的索引名（见设计 1）
LINK_FIELDS = ("download", "release")
DIST_DIR = os.path.join(LIB_ROOT, "dist")      # zip 落点（已 gitignore）
probe_cache: dict = {}                         # 直链 -> Content-Length（同一直链只 HEAD 一次）

REPO = os.environ.get("VOXMODELS_REPO") or "hengshizhi/voxmodels"

# `gh` 的**网络类**错误特征串（命中才重试；非网络错误要立刻返回，否则白等几十秒）。
# 实测本机到 GitHub API 抖动明显（三次里失败一次），且失败形态不止一种。
RETRY_ERR = ("TLS handshake timeout", "failed to receive handshake", "TLS handshake",
             "connection attempt failed", "connection reset", "connection timed out",
             "i/o timeout", "unexpected EOF", "EOF", "502", "503", "504")
# 这些状态算"办成了"（链接可信、可以回填）；其余一律算失败。
GOOD_STATUS = ("已发布", "已存在", "覆盖", "草稿已发布", "补传资产")

KIND_CN = {"base": "基础模型", "derivation": "微调模型", "timbre": "音色"}
INSTALL_CN = {
    "base": "解压后把 `<Name>/` 整个放到 MeanVC2 的 `finetune/exports/<Name>/`。",
    "derivation": "解压后把 `<Name>/` 整个放到 MeanVC2 的 `finetune/exports/<Name>/`。",
    "timbre": ("解压后把 `<Name>/` 整个放到 MeanVC2 的任一音色根（`app/presets/` 或 "
               "`finetune/anchors_speech/` 等）。**它只对上面列出的适配模型有效** —— "
               "锚点是针对某一份权重的响应面优化的，换权重必须重建。"),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def find_meanvc2(cli: str = "") -> str:
    """定位 MeanVC2 主仓（只是用来找本体文件，没有它也能靠 `--src` 工作）。"""
    for c in (cli, os.environ.get("MEANVC2_DIR", ""),
              os.path.join(os.path.dirname(LIB_ROOT), "MeanVC2"),
              os.path.join(os.path.dirname(LIB_ROOT), "VoxMage")):
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return ""


# ------------------------------------------------------------------ 读写索引
def read_json(path: str, default=None):
    """读 JSON。用 `utf-8-sig` 而不是 `utf-8`：**容许 BOM**。

    实测在 Windows 上用 `Set-Content`/记事本改一个索引就会带上 BOM，而 `json.load` 遇 BOM
    直接抛错 ⇒ "文件明明在"却读不出来（下一步就可能把已回填的发布链接抹掉）。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:                      # noqa: BLE001
        log(f"  [warn] 读取 {path} 失败：{e}")
        return default


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:     # 注意：**不写 BOM**
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def index_path(category: str, name: str) -> str:
    return os.path.join(LIB_ROOT, category, name + INDEX_EXT)


def all_entries() -> dict:
    """`(category, name) -> 索引内容`。"""
    out = {}
    for cat in CATEGORIES:
        d = os.path.join(LIB_ROOT, cat)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(INDEX_EXT):
                continue
            man = read_json(os.path.join(d, fn), {}) or {}
            out[(cat, fn[:-len(INDEX_EXT)])] = man
    return out


# ------------------------------------------------------------------ 定位本体
def payload_dirs(category: str, name: str, meanvc2: str) -> list:
    """本体文件可能在的目录（按可能性排序）。"""
    if not meanvc2:
        return []
    if category == "timbre":
        return [os.path.join(meanvc2, "app", "presets", name),
                os.path.join(meanvc2, "finetune", "anchors_speech", name),
                os.path.join(meanvc2, "finetune", "anchors_ft", name),
                os.path.join(meanvc2, "finetune", "anchors_models", name)]
    # 模型：正常在 exports/<Name>/；官方基座是裸权重（没有自己的目录），躺在 pretrained_models 里
    return [os.path.join(meanvc2, "finetune", "exports", name),
            os.path.join(meanvc2, "ckpts", "pretrained_models")]


def find_payload(category: str, name: str, man: dict, *, meanvc2: str, src: str = ""):
    """返回 `(本体路径, 它所在的目录)`。

    文件名取自索引的 `weights` / `vector` 字段（**不是猜文件名**）—— 官方基座那种
    "一个目录里躺着多个权重"的情况，只有靠字段才不会拿错。
    """
    field = "vector" if category == "timbre" else "weights"
    fn = str(man.get(field) or "").strip()
    if not fn:
        raise SystemExit(f"{name}: 索引里没有 `{field}` 字段")
    if os.path.isabs(fn) and os.path.isfile(fn):
        return fn, os.path.dirname(fn)
    dirs = ([src] if src else []) + payload_dirs(category, name, meanvc2)
    for d in dirs:
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p, d
    raise SystemExit(f"{name}: 找不到本体 `{fn}`。找过：\n    "
                     + "\n    ".join(dirs)
                     + "\n  用 --src <目录> 指定，或设环境变量 MEANVC2_DIR")


def ensure_cover(stage: str, man: dict, src_dir: str, meanvc2: str) -> bool:
    """封面：本体旁边有 `Cover.png` 就复制；没有就现场生成（官方基座本来没有封面）。"""
    c = os.path.join(src_dir, "Cover.png")
    if os.path.isfile(c):
        shutil.copy2(c, os.path.join(stage, "Cover.png"))
        return True
    if not meanvc2:
        return False
    try:
        if meanvc2 not in sys.path:
            sys.path.insert(0, meanvc2)
        from finetune import make_cover
        make_cover.make(stage, man.get("name") or "", make_cover._subtitle(man))
        return os.path.isfile(os.path.join(stage, "Cover.png"))
    except Exception as e:                      # noqa: BLE001
        log(f"  [warn] 封面生成失败（不影响发布）：{e}")
        return False


# ------------------------------------------------------------------ 打包
def tag_for(category: str, name: str) -> str:
    """release tag = `<类>-<Name>`，一眼能对上是哪个资产。"""
    return f"{category}-{name}"


def url_pair(category: str, name: str, repo: str) -> tuple:
    """`(下载直链, 发布页)`。"""
    tag = tag_for(category, name)
    return (f"https://github.com/{repo}/releases/download/{tag}/{name}.zip",
            f"https://github.com/{repo}/releases/tag/{tag}")


def pack(category: str, name: str, man: dict, *, meanvc2: str, src: str = "") -> dict:
    """打成 `dist/<Name>.zip`（顶层 `<Name>/`，内含 `cf.json`）。返回摘要。"""
    payload, pdir = find_payload(category, name, man, meanvc2=meanvc2, src=src)

    stage = os.path.join(DIST_DIR, name)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)

    write_json(os.path.join(stage, PACK_INDEX_NAME), man)      # 索引改名回 cf.json（设计 1）
    shutil.copy2(payload, os.path.join(stage, os.path.basename(payload)))
    cover = ensure_cover(stage, man, pdir, meanvc2)

    zip_path = os.path.join(DIST_DIR, name + ".zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in sorted(os.listdir(stage)):    # 顶层带 <Name>/，与 exports/<Name>/ 对齐
            z.write(os.path.join(stage, fn), arcname=f"{name}/{fn}")
    # 全量 sha256（客户端要拿它校验下载到的 zip；`sha` 只留短摘要给人看）。
    # 分块读：权重 86 MB，一次性 read() 会白占一份内存。
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    full = h.hexdigest()
    return {"zip": zip_path, "size": os.path.getsize(zip_path), "sha": full[:16],
            "sha256": full, "cover": cover}


# ------------------------------------------------------------------ 发布
def gh(*args: str, retries: int = 4) -> subprocess.CompletedProcess:
    """跑 `gh`，对**网络类**错误重试（指数退避）。见 RETRY_ERR 的注释。"""
    last = None
    for i in range(retries):
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return r
        blob = (r.stderr or "") + (r.stdout or "")
        if not any(e.lower() in blob.lower() for e in RETRY_ERR):
            return r                    # 非网络错误（如 tag 不存在）⇒ 立即返回，别白等
        last = r
        time.sleep(2 * (i + 1))
    return last


def release_state(tag: str, repo: str):
    """`(状态, 资产数)`；状态 ∈ `True`(已发布) / `"draft"` / `False`(没有) / `None`(问不出来)。

    **草稿必须单独算一类**：它是上次被打断（取消 / 断网）留下的 —— 对外不可见、直链 **404**。
    把草稿当"已存在"就会回填一个死链（实测踩过：`derivation-voxmage-ft-1.1` 草稿 assets 为空、
    直链 404，而正常发布的同族返回 200）。

    也必须把 `False` 与 `None` 分开：把网络错误当"不存在"会让脚本去 `create`，
    撞名时白跑一趟，还把"网络不通"误报成"创建失败"。问不出来就什么都不做，让人重跑。
    """
    r = gh("release", "view", tag, "--repo", repo, "--json", "isDraft,assets")
    if r.returncode == 0:
        try:
            d = json.loads(r.stdout)
        except Exception:                       # noqa: BLE001
            return True, -1
        return ("draft" if d.get("isDraft") else True), len(d.get("assets") or [])
    blob = ((r.stderr or "") + (r.stdout or "")).lower()
    if "not found" in blob or "no releases" in blob:
        return False, 0
    return None, 0


def notes_for(category: str, man: dict) -> str:
    """release 正文（给下载者看的）：这是什么、上一级是谁、装哪儿。"""
    L = [f"**{man.get('name')}** — {KIND_CN[category]}", ""]
    if man.get("base"):
        L.append(f"- 上一级权重：`{man['base']}`")
    if man.get("arch"):
        L.append(f"- 架构：`{man['arch']}`")
    if man.get("models"):
        L.append(f"- 适配模型：`{'`, `'.join(str(m) for m in man['models'])}`")
    if man.get("source"):
        L.append(f"- 来源语料：{man['source']}")
    if man.get("note"):
        L += ["", man["note"]]
    L += ["", "## 安装", "", INSTALL_CN[category], "",
          f"zip 内的索引是 `{PACK_INDEX_NAME}`（不是库里的 `<Name>.json`），"
          "解压后**不需要任何改名**即可被应用识别。"]
    return "\n".join(L)


def publish(category: str, name: str, man: dict, *, repo: str, force: bool) -> tuple:
    """上传并回填链接。返回 `(状态, 说明)`。"""
    tag = tag_for(category, name)
    st = release_state(tag, repo)
    if st is None:
        return "网络未通", f"查不到 {tag} 的状态 ⇒ 没上传也没回填（重跑即可）"

    if st == "draft":
        # 补资产 + 取消草稿。顺序不能反：先转正再补资产的话，中间那段时间直链是 404 的。
        up = gh("release", "upload", tag, "--repo", repo, "--clobber",
                os.path.join(DIST_DIR, name + ".zip"))
        if up.returncode != 0:
            return "草稿修复失败", (up.stderr or "").strip()[:150]
        ed = gh("release", "edit", tag, "--repo", repo, "--draft=false")
        if ed.returncode != 0:
            return "草稿修复失败", "资产已补传，但草稿没转正：" + (ed.stderr or "").strip()[:120]
        return "草稿已发布", f"{tag}（上次被打断留下的草稿，已补资产并转正）"

    if st is True:
        if nassets == 0:
            # 已发布但**没有资产** —— 直链同样是死的。补传，不重复建 release。
            up = gh("release", "upload", tag, "--repo", repo, "--clobber",
                    os.path.join(DIST_DIR, name + ".zip"))
            return ("补传资产" if up.returncode == 0 else "补传失败"), (up.stderr or "").strip()[:150]
        if not force:
            return "已存在", ""            # 已有且完好 ⇒ 当作已发布，照样回填（设计 5）
        up = gh("release", "upload", tag, "--repo", repo, "--clobber",
                os.path.join(DIST_DIR, name + ".zip"))
        return ("覆盖" if up.returncode == 0 else "覆盖失败"), (up.stderr or "").strip()[:120]
    r = gh("release", "create", tag, "--repo", repo, "--title", name,
           "--notes", notes_for(category, man), os.path.join(DIST_DIR, name + ".zip"))
    if r.returncode != 0:
        return "失败", (r.stderr or r.stdout or "").strip()[:200]
    return "已发布", ""


def backfill(category: str, name: str, repo: str, extra: dict | None = None) -> bool:
    """把两个链接（+ 可选的 `sha256` / `size`）写回库索引，保留其余字段与顺序。

    `extra` 只在**我们真的上传了**这份 zip 时才传 —— 见 `UPLOADED_STATUS`：
    "已存在"（release 里已有资产、我们没传）时远端那份和我们刚打的不一定同一份，
    写进去的 sha 就会指向另一个文件，客户端校验必挂。宁可空着（客户端跳过校验）。
    """
    p = index_path(category, name)
    man = read_json(p, None)
    if man is None:
        return False
    d_url, r_url = url_pair(category, name, repo)
    changed = False
    if man.get("download") != d_url:
        man["download"] = d_url
        changed = True
    if man.get("release") != r_url:
        man["release"] = r_url
        changed = True
    for k, v in (extra or {}).items():
        if v and man.get(k) != v:
            man[k] = v
            changed = True
    if not changed:
        return False                       # 已是这个值，不白改文件
    write_json(p, man)
    return True


def probe_size(url: str, timeout: int = 20) -> int:
    """对一个发布直链发 HEAD，取 `Content-Length`（失败返回 0）。

    给**早于本字段**的条目补 `size` 用：客户端进度条要总字节数，而重新下载 17 个资产
    （1.4 GB）只为算 sha 不值当；大小能白拿就先拿。
    """
    import urllib.request
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "voxmodels-release"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception as e:                      # noqa: BLE001
        log(f"  [warn] HEAD 失败 {url[:60]}…：{e}")
        return 0


# 真正**上传过**的状态（只有这些状态下的 sha256 / size 才可信，见 backfill 的注释）
UPLOADED_STATUS = ("已发布", "草稿已发布", "补传资产", "覆盖")


def write_aggregate_index(repo: str, *, probe: bool = False) -> dict:
    """把 238 个分散索引聚成一个 `index.json`（客户端只请求这一个文件）。

    为什么必须有它：GitHub 的 **API 在本机被 403**（限流/被挡），客户端没法枚举目录；
    而 raw 单文件实测 1.7 s 能拿到。238 次 raw 请求既慢又容易被限。
    """
    import datetime as _dt
    ents = all_entries()
    items = []
    for (cat, name) in sorted(ents, key=lambda k: (CATEGORIES.index(k[0]), k[1])):
        man = ents[(cat, name)]
        it = {"category": cat, "name": man.get("name") or name}
        for k, v in man.items():
            if k not in it:
                it[k] = v
        if probe and not it.get("size") and it.get("download"):
            if not probe_cache.get(it["download"]):
                probe_cache[it["download"]] = probe_size(it["download"])
                if probe_cache[it["download"]]:
                    log(f"  [probe] {cat}/{name:<32} {probe_cache[it['download']]/1e6:8.2f} MB")
            if probe_cache.get(it["download"]):
                it["size"] = probe_cache[it["download"]]
        items.append(it)
    out = {
        "schema": 1,
        "repo": repo,
        "generated": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(items),
        "entries": items,
    }
    p = os.path.join(LIB_ROOT, "index.json")
    write_json(p, out)
    with_sha = sum(1 for i in items if i.get("sha256"))
    with_url = sum(1 for i in items if i.get("download"))
    with_size = sum(1 for i in items if i.get("size"))
    log(f"\n聚合索引已写入 {p}")
    log(f"  {len(items)} 条：有直链 {with_url} / 有大小 {with_size} / 有 sha256 {with_sha}")
    log("  ⚠️ 记得提交推送：git add -A && git commit -m 'chore: 更新聚合索引 index.json' && git push")
    return out


# ------------------------------------------------------------------ main
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="模型库条目打包 / 发布到 GitHub Release")
    ap.add_argument("--list", action="store_true", help="列出条目与发布状态")
    ap.add_argument("--kind", default="", help="base / derivation / timbre")
    ap.add_argument("--name", default="", help="条目名（逗号分隔可多个）")
    ap.add_argument("--all", action="store_true",
                    help="确认处理当前筛选出的**全部**条目（不带它就必须给 --name/--match）")
    ap.add_argument("--publish", action="store_true", help="真正上传到 GitHub（默认只打包）")
    ap.add_argument("--force", action="store_true", help="release 已存在时覆盖上传")
    ap.add_argument("--repo", default=REPO, help=f"owner/repo（默认 {REPO}）")
    # 筛选**故意做成命令行参数**而不是写死的规则：某一批想发什么是一时的取舍
    # （如"这轮只发 AISHELL 的、且不发 @pp 的"），写进代码会变成一条假规矩。
    ap.add_argument("--match", default="", help="只处理名字匹配该正则的条目（re.search）")
    ap.add_argument("--exclude", default="", help="跳过名字匹配该正则的条目")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理几个（防手滑）")
    ap.add_argument("--meanvc2", default="", help="MeanVC2 主仓路径（找本体用）")
    ap.add_argument("--src", default="", help="直接指定本体所在目录（优先于搜索）")
    ap.add_argument("--index", action="store_true",
                    help="生成聚合索引 index.json（238 条合成一个文件，客户端只请求它）")
    ap.add_argument("--probe", action="store_true",
                    help="配合 --index：对缺 size 的条目发 HEAD 补大小（早于该字段的老条目）")
    args = ap.parse_args()

    if args.index:
        write_aggregate_index(args.repo, probe=args.probe)
        return 0

    ents = all_entries()
    if args.kind:
        ents = {k: v for k, v in ents.items() if k[0] == args.kind}
    if args.match:
        rx = re.compile(args.match)
        ents = {k: v for k, v in ents.items() if rx.search(k[1])}
    if args.exclude:
        rx = re.compile(args.exclude)
        ents = {k: v for k, v in ents.items() if not rx.search(k[1])}
    keys = sorted(ents, key=lambda k: (CATEGORIES.index(k[0]), k[1]))

    if args.list:
        log(f"仓库 {args.repo}    库根 {LIB_ROOT}")
        log(f"{'类别':<11}{'条目':<32}发布")
        for k in keys:
            man = ents[k]
            mark = "✓ " + tag_for(*k) if man.get("download") else "—"
            log(f"{k[0]:<11}{k[1]:<32}{mark}")
        log(f"\n共 {len(keys)} 条")
        return 0

    if not keys:
        log("没有匹配的条目")
        return 2
    # 安全闸：不带任何筛选就"默认全发"（238 条）是一发不可收拾的操作。
    if not (args.all or args.name or args.match or args.exclude):
        log("要明确范围：加 --all（配合 --kind / --match / --exclude 筛选），或 --name 指定条目。")
        log(f"（当前不加限制会命中全部 {len(keys)} 条）")
        return 2
    if args.name:
        want = [s.strip() for s in args.name.split(",") if s.strip()]
        keep = [k for k in keys if k[1] in want]
        missing = [n for n in want if not any(k[1] == n for k in keys)]
        if missing:
            log(f"⚠️ 没有这些条目：{', '.join(missing)}")
        keys = keep
        if not keys:
            return 2
    if args.limit:
        keys = keys[:args.limit]

    meanvc2 = find_meanvc2(args.meanvc2)
    os.makedirs(DIST_DIR, exist_ok=True)
    log(f"仓库 {args.repo}    MeanVC2 {meanvc2 or '(未找到，需要 --src)'}    "
        f"{'发布' if args.publish else '只打包（未加 --publish）'}")
    log()
    ok = fail = 0
    for cat, name in keys:
        try:
            info = pack(cat, name, ents[(cat, name)], meanvc2=meanvc2, src=args.src)
        except SystemExit as ex:
            log(f"  {cat}/{name:<30} 打包失败：{ex}")
            fail += 1
            continue
        line = (f"  {cat}/{name:<30} {info['size']/1e6:8.2f} MB  sha {info['sha']}  "
                f"{'有封面' if info['cover'] else '无封面'}")
        if not args.publish:
            log(line)
            ok += 1
            continue
        status, detail = publish(cat, name, ents[(cat, name)], repo=args.repo, force=args.force)
        # sha256 / size 只在**真的上传了**这份 zip 时回填（见 backfill 的注释）
        extra = ({"sha256": info["sha256"], "size": info["size"]}
                 if status in UPLOADED_STATUS else None)
        wrote = backfill(cat, name, args.repo, extra) if status in GOOD_STATUS else False
        log(f"{line}  [{status}]{'  链接已回填' if wrote else ''}")
        if detail:
            log(f"      {detail}")
        ok += status in GOOD_STATUS
        fail += status not in GOOD_STATUS

    log()
    log(f"完成：成功 {ok} / 失败 {fail}")
    if args.publish and ok:
        # 设计 4：回填只改了工作区 —— 不提交的话 GitHub 上看到的两个字段还是空的。
        log("⚠️ 记得提交并推送回填结果：")
        log("     git add -A && git commit -m 'chore: 回填发布链接' && git push")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
