# Pebble 简体中文语言包（思源黑体）

专为 Pebble Time 2（及 PebbleOS v4.19+ 固件）打造的简体中文语言包，解决官方语言包宋体字形裁顶、社区包字号偏小且无真粗体的问题。

当前版本：**v11**，请到 [Releases](../../releases) 下载 `pebble-zh-CN-notosans-v11.pbl`。

![预览](preview-zh.png)

## 字体

- **思源黑体（Noto Sans SC）**，共 **11 个非空字体槽**：
  - `GOTHIC_14 / 18 / 24 / 28 / 36` 常规 + 真粗体（10 槽，汉字字高 13 / 16 / 19 / 22 / 26 px）
  - `ROBOTO_CONDENSED_21` 常规（Slot 19，汉字字高 18 px）
- 每槽 **10,398 个字形**：完整覆盖 GB2312 全部汉字 + Big5 常用繁体字，语音听写出现繁体变体字（如「氣」「麼」）也不再显示豆腐块
- 字形基线按 PebbleOS 官方英文字体精确对齐，不裁顶、不漂移，与拉丁字母同基线齐平
- 保留系统原生 LECO 数字摘要卡片风格（睡眠 / 心率摘要卡显示 `6h 22min`、`93 BPM`，不打包 LECO 扩展槽）

## 翻译

- 完整覆盖最新 `coredevices/PebbleOS` `main` 分支全部 `pbl_i18n_*` 系统词条，并兼容旧版固件词条，共 **955 条有效译文**（MO 含元数据头共 956 条）
- 系统提示称呼统一用「您」；术语与细节经多轮校对（如 `Timeout / TIMEOUT` 统一译为「显示时长」以兼顾背光与通知两处菜单、`Motion Backlight` 译为「抬手背光」以区分「动态背光」、心率统一 `BPM`、睡眠洞察统一 `%d小时 %d分`、窄统计卡片时长保留紧凑 `%dH %dM` 避免截断、锻炼 / 触控术语统一），无空条目、无格式符错误

## 验证

- 打包 STM32 CRC-32（整包与全部 19 个槽位）全部通过，零尾字节
- 按固件 `subsys/i18n/i18n.c` 的 PJW 哈希查表算法离线模拟，956 / 956 全部命中
- 全部译文中的非 Emoji 中文与标点字符 100% 命中 11 个字体槽
- 已在 Pebble Time 2 真机验证显示正常

## 安装与硬件兼容

- **适用机型**：推荐 Pebble Time 2（Obelix）/ Pebble Round 2（Getafix）等配备 32 MB Flash 的新款机型。语言包体积约 **6.0 MiB（6.28 MB）**，Flash 空间较小的老款 Pebble 机型可能无法安装。
- **安装步骤**：
  1. 到 [Releases](../../releases) 下载 `pebble-zh-CN-notosans-v11.pbl`
  2. 用 Pebble 手机 App 打开该文件即可安装；如装过其他中文包，先在 App 里删掉旧包再装
  3. 安装后在语言设置里选择「简体中文」

> 注：第三方表盘 / 应用自身的文字、手机推送的通知正文不在系统语言包范围内，任何语言包都无法翻译第三方应用文案（但本包的思源黑体字库可正常渲染其中的中文）。

## 源码与自行构建

本仓库开源了全部翻译源文件与打包 / 校验脚本，欢迎提 Issue 或 PR 完善译文：

- [`po/zh_CN.po`](po/zh_CN.po)：简体中文翻译源文件（GNU gettext PO）
- [`scripts/build_pbl.py`](scripts/build_pbl.py)：编译 `.po` 并打包 `.pbl`（按 PebbleOS `stm32_crc` 规范计算 CRC-32）
- [`scripts/verify_pbl.py`](scripts/verify_pbl.py)：离线校验 `.pbl` 的 CRC、MO 字典序、PJW 哈希表、格式符、字形覆盖率及 PebbleOS 源码词条覆盖率
- [`scripts/render_preview.py`](scripts/render_preview.py)：直接用 `.pbl` 内的真实 PBF 点阵渲染 `preview-zh.png`

示例（复用任意已有 `.pbl` 中的 11 个思源黑体字库槽重新打包）：

```bash
python3 scripts/build_pbl.py \
  --po po/zh_CN.po \
  --base-pbl pebble-zh-CN-notosans-v10.pbl \
  --out dist/pebble-zh-CN-notosans-v11.pbl

python3 scripts/verify_pbl.py dist/pebble-zh-CN-notosans-v11.pbl
```

## 许可

- 字体 Noto Sans SC © Google，遵循 SIL Open Font License 1.1
- 翻译与脚本：MIT（详见 [`LICENSE`](LICENSE)）
