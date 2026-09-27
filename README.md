# voxmodels 模型库（索引仓）

本仓库**只存索引**。模型本体（`.safetensors` 权重 / `.npy` 音色向量）**不进这个仓库**，
而是通过 **GitHub Release** 的 zip 分发 —— **一次发布 = 一个模型或一个音色**。

这样做的原因：权重单个 67–89 MB、音色有数百份，塞进 git 会让仓库膨胀到不可用；
而索引是几十 KB 的纯文本，进 git 才能 diff、才能被人检索。

---

## 1. 目录结构

```
voxmodels/
├─ base/         基础模型    <Name>.json
├─ derivation/   微调模型    <Name>.json
└─ timbre/       音色        <Name>.json
```

**索引平铺**在所属类目录下，文件名 = 条目名。不要套 `<Name>/` 子目录 ——
库只存索引，套一层子目录装不出别的东西（权重在 release 的 zip 里）。

## 2. 三类怎么分

| 目录 | 含义 | 判定依据 |
|---|---|---|
| `base/` | 基础模型：谱系上最上游的那一代 | `base` 字段指向**本仓库之外**（官方上游基座，如 `meanvc2-120ms-1.0`）|
| `derivation/` | 微调模型 | `base` 字段指向**本仓库内**另一个模型 |
| `timbre/` | 音色（声纹锚点）| — |

> ⚠️ **不要用 `kind` 字段判断分类**。实测所有模型的 `kind` 都是 `base_model`（连微调出来的
> `atlas-v01` 也是）。`kind` 被应用的 GUI 当**显示字符串**用（`app/train.py` 的 `TARGETS`），
> 所以它保持原值不动；分类只体现在**目录名**上。

## 3. 索引字段

### 3.1 模型（`base/`、`derivation/`）

| 字段 | 必填 | 含义 |
|---|---|---|
| `name` | ✓ | 条目名，必须与文件名一致 |
| `kind` | ✓ | 固定 `base_model`（见上面 ⚠️）|
| `weights` | ✓ | 权重文件名（zip 内路径），如 `atlas-v01.safetensors` |
| `base` | ✓ | 父权重名。**库外** ⇒ 本条目属 `base/`；**库内** ⇒ 属 `derivation/` |
| `arch` | | 架构 id，指向 MeanVC2 的 `finetune/architectures/<name>/`。老权重可能没有 |
| `trainer` / `timbre_trainer` / `timbre_loader` / `timbre_adapter` | | 训练与音色加载用的注册表条目名 |
| `aliases` | | 兼容别名，`app/models.py` 按名解析时会认 |
| `step` / `created` / `note` | | 步数 / 导出日期 / 备注 |
| `download` | ✓ | **发布 zip 的直链**（GitHub release asset）|
| `release` | ✓ | **发布页**，给人看的 |

### 3.2 音色（`timbre/`）

| 字段 | 必填 | 含义 |
|---|---|---|
| `name` | ✓ | 条目名，必须与文件名一致（重建过的音色带 `@<代号>` 后缀，见 §5）|
| `kind` | ✓ | 固定 `timbre` |
| `vector` | ✓ | 音色向量文件名，如 `冷冷v2.npy` |
| `models` | ✓ | **这份音色适配哪些模型**（列表）。用别的模型加载会警告 |
| `base` | ✓ | **训练它的那个模型**的 `base` 字段值（**只走一步**，不递归）。例：`AISHELL-SSB0011@atlas-v01` 的 `models=["atlas-v01"]`、`atlas-v01.base="voxmage-ft-1.1"` ⇒ 本字段 = `voxmage-ft-1.1` |
| `timbre_loader` / `timbre_adapter` | | 音色加载器 / 适配器 name |
| `source` / `gender` / `f0_hz` / `anchor_gain` | | 来源语料 / 性别 / 基频中位 / 锚点优化增益（展示用）|
| `timbre_trainer` / `created` / `created_by` / `trained` / `pending` | | 训练器与时间戳（GUI 新建的音色才有）|
| `note` | ✓ | 备注（可以是空字符串，但字段必须在）|
| `download` / `release` | ✓ | 同模型 |

## 4. 怎么**下载安装**一个模型或音色

1. 打开 `release`（发布页），或直接用 `download` 直链下载 zip。
2. 解压，得到：

   ```
   <Name>/
   ├─ cf.json          ← 注意：**zip 里的索引叫 cf.json**
   ├─ <Name>.safetensors（模型）或 <Name>.npy（音色）
   └─ Cover.png
   ```

   > zip 内是 `cf.json`、库里是 `<Name>.json` —— 这是**刻意**的。应用按 `cf.json` 读
   > （`app/models.py`、`finetune/registry.py` 都硬编码这个名字），所以解压后**改都不用改**。

3. 放进 MeanVC2 对应目录：

   | 类型 | 目标目录 |
   |---|---|
   | 模型 | `finetune/exports/<Name>/` |
   | 音色 | 任一音色根：`app/presets/`（推荐，用户装的）、`finetune/anchors_speech/`（说话人）、`finetune/anchors_ft/`（歌手）、`finetune/anchors_models/`（按权重重建的） |

4. **音色必须在它 `models` 列出的那份权重上使用**。锚点是针对某一份权重的响应面优化出来的，
   换权重会明显退化 —— 这不是"兼容性小问题"，是设计如此（换权重必须重建锚点）。

## 5. 怎么**发布**一个模型或音色

1. 在 MeanVC2 里把资产做成合规目录（模型见其 README §9.4，音色见 §9.3）。
2. 同步索引进本仓库（在 **MeanVC2** 目录里跑）：

   ```bash
   python finetune/voxmodels.py            # 只看计划，不写盘
   python finetune/voxmodels.py --write    # 落盘
   ```

3. 在本仓库根目录用 **`release.py`** 打包并发布（它自动打 zip、建 release、回填链接）：

   ```bash
   python release.py --list                                     # 看有哪些、发布没发布
   python release.py --kind timbre --name 冷冷v2                  # 只打包，不上传
   python release.py --kind timbre --name 冷冷v2 --publish
   python release.py --kind timbre --all --match '^AISHELL' --exclude '@pp' --publish
   ```

   常用开关：`--match` / `--exclude`（正则筛选，**某一批发什么是取舍、不是规矩，所以放命令行**）、
   `--limit`、`--force`（release 已存在时覆盖上传）、`--src`（本体目录找不到时直接指定）。
   筛选参数一个都不给时脚本会拒绝执行 —— 免得"默认全发 238 条"。

4. **把回填结果提交推送**：

   ```bash
   git add -A && git commit -m "chore: 回填发布链接" && git push
   ```

   > ⚠️ 这一步不能省。`release.py` 只改工作区；**不提交的话，GitHub 上看到的 `download` /
   > `release` 还是空的**（踩过）。

**约定**：一次发布 = 一个模型或音色，tag 用 `<类>-<Name>`（如 `derivation-atlas-v01`、
`timbre-冷冷v2`），zip 内保持 `<Name>/cf.json` 的形态 —— 都由 `release.py` 自动做到。
弱网下失败的条目**重跑一遍**即可：已有 release 会被识别为"已存在"，只补回填、不重传。

## 6. 维护约定（踩过的坑）

- **`download` / `release` 由人回填，同步工具不会覆盖它们**。`finetune/voxmodels.py` 是
  **合并**而非覆盖：其余字段以 MeanVC2 本机那份 `cf.json` 为准，这两个字段保留库里已有的值。
  否则每同步一次就得把链接重填一遍。
- **别用会加 BOM 的方式存 JSON**（记事本 "UTF-8"、PowerShell `Set-Content` 默认都加）。
  同步工具读得进 BOM，但**打进 zip 后应用读不出**（应用用的是严格的 `utf-8`）。
  用工具写（它不加 BOM），或用 VS Code 并确认编码为 "UTF-8"（不是 "UTF-8 with BOM"）。
- **JSON 里要写引号就用「」**，别用直的 `"`。曾经有一张卡因为 note 里写了直引号而
  JSON 断裂，整张卡读不出来。
- 重建过的音色带 `@<代号>` 后缀（如 `AISHELL-SSB0005@ft-1.1`）：**这是有意的**，
  同一个音色在 7 份不同权重上有 7 份互不兼容的锚点，去掉后缀会撞名。
- 库里有、本机没有的条目**不会被工具自动删**（可能是别人发布来的资产），只会报出来让人判断。

## 7. 工具在哪

| 工具 | 位置 | 干什么 |
|---|---|---|
| **`release.py`** | **本仓库根目录** | 打包 zip → 建 GitHub Release → 回填 `download` / `release`。自包含，只读本仓索引。 |
| `finetune/voxmodels.py` | MeanVC2 主仓 | 索引同步与体检（本机资产 → 库里的 `<Name>.json`）。它要扫 MeanVC2 的资产目录，所以留在那边。 |

```bash
python release.py --list                        # 本仓库：列出条目与发布状态
python release.py --kind base --name atlas-v01 --publish

cd <MeanVC2>
python finetune/voxmodels.py --quiet            # 只列需要人看的
python finetune/voxmodels.py --only timbre      # 只同步音色
python finetune/voxmodels.py --check            # 体检（命名 / name 一致性 / 链接格式 / 音色 base）
```

`release.py` 需要知道**本体文件**在哪（本仓只存索引）。它按序搜索 MeanVC2 的资产目录；
可用 `--src <目录>` 直接指定，或设 `MEANVC2_DIR` 环境变量。库根默认取"MeanVC2 仓库的
同级 `voxmodels/`"，可用 `VOXMODELS_DIR` 覆盖（`voxmodels.py` 一侧）。
