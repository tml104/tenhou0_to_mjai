# tenhou0_to_mjai

把天凤 XML 牌谱（http://tenhou.net/0/）批量转换为 `mjai` 事件流格式的工具仓库。

当前仓库的重点是：

- 说明天凤 XML 牌谱的获取与字段含义
- 说明本项目使用的 `mjai` JSON Lines 格式
- 提供一个可并行执行的 Python 转换脚本，把按年份存放的天凤牌谱批量转换成 `mjai`

## 项目介绍

天凤原始牌谱是 XML 格式，而很多后续分析、数据处理或对接 `mjai` 生态的工具更适合消费“按时间顺序逐行输出 JSON 事件”的日志格式。本项目的目标就是把这两种表示打通。

当前脚本会读取天凤四人麻将牌谱中的主要事件，并输出为 `mjai` 事件流，包括：

- `start_game`
- `start_kyoku`
- `tsumo`
- `dahai`
- `chi`
- `pon`
- `daiminkan`
- `kakan`
- `ankan`
- `dora`
- `reach`
- `reach_accepted`
- `hora`
- `ryukyoku`
- `end_kyoku`
- `end_game`

项目中的参考文档：

- [docs/天凤牌谱采集分析.md](/home/tml104/tenhou0_to_mjai/docs/天凤牌谱采集分析.md)
- [docs/mjai.md](/home/tml104/tenhou0_to_mjai/docs/mjai.md)

## 目录结构

当前仓库建议按下面的方式组织数据：

```text
tenhou0_to_mjai/
├── docs/
│   ├── mjai.md
│   └── 天凤牌谱采集分析.md
├── paipu/
│   ├── paipu-2018/
│   ├── paipu-2019/
│   ├── ...
│   └── paipu-2025/
├── paipu-mjai/
│   ├── paipu-mjai-2018/
│   ├── paipu-mjai-2019/
│   ├── ...
│   └── paipu-mjai-2025/
└── scripts/
    └── convert_tenhou_xml_to_mjai.py
```

其中：

- `paipu/paipu-YYYY/` 存放天凤 XML 牌谱
- `paipu-mjai/paipu-mjai-YYYY/` 存放转换后的 `mjai` 文件
- 脚本会递归扫描输入目录下的 `.xml` 文件
- 输出目录会自动创建

## 环境要求

- Python 3.10+
- 仅使用 Python 标准库，无需额外安装第三方依赖

## 输入与输出约定

### 输入

脚本默认按年份读取下面这些目录：

- `./paipu/paipu-2018`
- `./paipu/paipu-2019`
- ...
- `./paipu/paipu-2025`

每个目录下可以继续有子目录，脚本会递归查找全部 `.xml` 文件。

### 输出

输出目录与年份一一对应：

- `./paipu-mjai/paipu-mjai-2018`
- `./paipu-mjai/paipu-mjai-2019`
- ...
- `./paipu-mjai/paipu-mjai-2025`

输出文件会保留输入文件的相对路径结构，并把后缀替换为 `.json`。文件内容是 JSON Lines，也就是：

- 一行一个 `mjai` 事件
- 事件按真实对局顺序排列

示例：

```json
{"type":"start_game","names":["A","B","C","D"]}
{"type":"start_kyoku","bakaze":"E","dora_marker":"5s","kyoku":1,"honba":0,"kyotaku":0,"oya":0,"scores":[25000,25000,25000,25000],"tehais":[["1m","2m","3m","4m","5m","6m","7m","8m","9m","E","S","W","N"],["?","?","?","?","?","?","?","?","?","?","?","?","?"],["?","?","?","?","?","?","?","?","?","?","?","?","?"],["?","?","?","?","?","?","?","?","?","?","?","?","?"]]}
{"type":"tsumo","actor":0,"pai":"1p"}
{"type":"dahai","actor":0,"pai":"1p","tsumogiri":true}
```

## 使用说明

### 1. 准备牌谱数据

先把天凤 XML 牌谱放到项目目录中的 `paipu/paipu-YYYY/` 下。例如：

```text
paipu/paipu-2025/sample1.xml
paipu/paipu-2025/subdir/sample2.xml
```

如果你还没有数据，可以先参考：

- [docs/天凤牌谱采集分析.md](/home/tml104/tenhou0_to_mjai/docs/天凤牌谱采集分析.md)

### 2. 执行转换

在项目根目录运行：

```bash
python scripts/convert_tenhou_xml_to_mjai.py
```

默认会处理 `2018` 到 `2025` 的全部年份目录。

如果只想处理部分年份，可以传 `--years`：

```bash
python scripts/convert_tenhou_xml_to_mjai.py --years 2025
python scripts/convert_tenhou_xml_to_mjai.py --years 2022,2024-2025
python scripts/convert_tenhou_xml_to_mjai.py --years 2018-2025
```

### 3. 调整并行度

脚本默认使用 `cpu_count - 1` 个 worker 进程。你也可以手动指定：

```bash
python scripts/convert_tenhou_xml_to_mjai.py --workers 8
```

对于几十万文件的批处理，必要时还可以调大 `chunksize`：

```bash
python scripts/convert_tenhou_xml_to_mjai.py --workers 8 --chunksize 200
```

### 4. 覆盖已存在输出

如果输出文件已存在，默认会跳过。要强制重写：

```bash
python scripts/convert_tenhou_xml_to_mjai.py --overwrite
```

### 5. 指定仓库根目录

脚本默认把自身上一级目录视为仓库根目录。若需要手动指定：

```bash
python scripts/convert_tenhou_xml_to_mjai.py --repo-root /path/to/tenhou0_to_mjai
```

## 运行时输出

批量转换过程中，脚本会周期性输出进度信息，包含：

- 已处理文件数
- 成功数
- 跳过数
- 失败数
- 当前吞吐速度
- 预计剩余时间

示例：

```text
[00:03:12] processed=12500/180000 ok=12480 skipped=0 error=20 rate=65.0 files/s eta=00:43:10
```

任务结束后会输出汇总报告，包括：

- 总耗时
- 总事件写入数
- 各年份发现文件数
- 各年份成功、跳过、失败数量
- 前若干条错误样本

## 当前实现的处理范围

脚本当前支持四人麻将 XML 牌谱，且会显式拒绝三麻牌谱。

已经覆盖的天凤 XML 标签包括：

- `GO`
- `UN`
- `TAIKYOKU`
- `INIT`
- `T/U/V/W`
- `D/E/F/G`
- `N`
- `DORA`
- `REACH`
- `AGARI`
- `RYUUKYOKU`

其中：

- `REACH step=1` 转成 `reach`
- `REACH step=2` 转成 `reach_accepted`
- `AGARI` 转成 `hora`
- `RYUUKYOKU` 转成 `ryukyoku`
- `sc` 字段会被解析成 `deltas`
- `doraHaiUra` 会被解析成 `ura_markers`

## 已知限制

当前实现是一个实用型批处理脚本，不是完整的天凤格式全量还原器。已知限制如下：

- 只支持四人麻将，不支持三麻
- 中途流局和普通荒牌流局目前都统一输出为 `ryukyoku`
- 天凤 `RYUUKYOKU` 的具体 `type`，例如 `yao9`、`kaze4`、`reach4`，当前没有单独保留到输出事件中
- 输出后缀当前是 `.json`，但文件内容本质上是 JSON Lines 形式的 `mjai` 事件流
- 仓库当前没有附带真实牌谱样本，因此还没有在本仓库内完成端到端数据验证

## 核心脚本

- 转换脚本：[scripts/convert_tenhou_xml_to_mjai.py](/home/tml104/tenhou0_to_mjai/scripts/convert_tenhou_xml_to_mjai.py)

如果后续需要扩展功能，通常会从这几个方向继续推进：

- 增加真实样本回归测试
- 为流局补充原因字段
- 补充更严格的副露编码校验
- 统一输出后缀命名约定

## 参考文档

- [docs/天凤牌谱采集分析.md](/home/tml104/tenhou0_to_mjai/docs/天凤牌谱采集分析.md)
- [docs/mjai.md](/home/tml104/tenhou0_to_mjai/docs/mjai.md)
