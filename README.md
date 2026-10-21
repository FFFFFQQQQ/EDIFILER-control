# EDIFIER W830NB 降噪控制(Windows)

为了实现与自有设备的互操作,对 EDIFIER ConneX App 的蓝牙通信协议做了协议分析,
据此在 Windows 上直接控制 W830NB 耳机的降噪模式,**无需手机 App**。

## 状态:✅ 已实机验证可用

五个档位全部验证通过(发送后回读确认):

```
深度降噪  OK      舒适降噪  OK      宽频  OK      防风噪  OK      环境声  OK
```

---

# 一、跑起来

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 1809+ / Windows 11(需 WebView2 运行时,Win11 自带) |
| Python | 3.10+(实测 3.13) |
| 耳机 | EDIFIER W830NB,已和这台电脑**配对过一次** |

> Win10 如果没有 WebView2,装一次微软官方的
> [Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)(几 MB)。

## 安装

```bash
git clone <repo> && cd <repo>
pip install -r requirements.txt
```

## 启动(源码)

```bash
python edifier_app.py
```

或双击 `启动控制台.bat`。

## 用法

- 启动后自动搜索附近的耳机,连上后把地址回填到「高级设置 → 设备」
- 点底部圆环切换档位,当前模式和电量实时显示
- 中间是真实音频的频谱柱(采集系统播放回环),没声音时为空
- 点右上角 ✕ 收进托盘,**托盘菜单 → 退出** 才真正结束进程

「高级设置」里还有:重连、查询状态、查电量、发原始帧、日志。

---

# 二、打包成 exe

```bash
pip install pyinstaller
```

然后双击 `打包exe.bat`,或手动执行:

```bash
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "W830NB-Console" ^
  --icon "ui/icon.ico" ^
  --add-data "ui;ui" ^
  --collect-submodules winrt ^
  --hidden-import pystray._win32 ^
  --exclude-module moderngl ^
  --exclude-module tkinter ^
  edifier_app.py
```

产物在 `dist/W830NB-Console.exe`,单文件约 36 MB。

**目标机器不需要装 Python**,但需要 WebView2 运行时(Win11 自带)。

几个参数不能省:

| 参数 | 为什么 |
|---|---|
| `--add-data "ui;ui"` | 界面 HTML 和图标,漏了会白屏 |
| `--collect-submodules winrt` | pywinrt 是动态加载的,PyInstaller 静态分析找不到 |
| `--hidden-import pystray._win32` | pystray 的 Windows 后端也是动态导入 |
| `--windowed` | 不弹控制台窗口 |

---

# 三、命令行版

```bash
python edifier_anc.py status                    # 查询当前模式
python edifier_anc.py set deep                  # 深度降噪
python edifier_anc.py set comfort               # 舒适降噪
python edifier_anc.py set wide                  # 宽频
python edifier_anc.py set wind                  # 防风噪
python edifier_anc.py set ambient               # 环境声
python edifier_anc.py cycle                     # 依次切换全部档位并回读验证
python edifier_anc.py raw "AA EC CC 00 00 62"   # 发原始帧
```

---

# 四、出问题时

**先看日志:** `%LOCALAPPDATA%\W830NB-Console\app.log`
里面记录了启动、音频设备、每次连接失败的具体错误码。

| 现象 | 处理 |
|---|---|
| 「特征枚举为空」/ 连不上 | Windows 的 GATT 缓存损坏(反复连断后常见)。把电脑蓝牙**关掉再打开**,再重连。程序会自动重试缓存/非缓存两种发现方式 |
| 搜索不到耳机 | 确认耳机已开机、没连着手机;或到「高级设置」手动填地址 |
| 频谱柱一直空、徽章显示「采集不可用」 | `pip install SoundCard` 没装上,或系统没有可用的播放设备 |
| 双击 exe 弹 SmartScreen | 没做代码签名,点「更多信息 → 仍要运行」 |
| exe 启动后白屏 20 秒 | 单文件 exe 每次要先解压到临时目录,属正常现象 |

---

# 五、协议规范(全部经实机逐帧验证)

## 帧格式

```
AA | EC | cmd | len_hi | len_lo | data... | crc

EC   = 0xEC  (AppCode,236 —— CommandAppCode.EC_CODE)
len  = len(data),大端 2 字节
crc  = (Σ(前面所有字节) ) & 0xFF
```

**载荷逐字节 XOR `0xA5` 加密**(APK 里的 `EncryptionCode.Encryption10`):

```java
// CommandCryptUtil.encryptPayload
byte key = EncryptionCode.Encryption10.getValue();   // 0xA5
for (i) out[i] = payload[i] ^ key;
```

## 降噪命令

| 操作 | cmd | 载荷(加密前) |
|---|---|---|
| 查询状态 | `0xCC` | 空 |
| 设置状态 | `0xC1` | `[groupIndex, 选项下标]` |

**查询响应:** `BB | EC | CC | 00 | 0n | 0xBE | 模式^0xA5 | [db^0xA5] | crc`
`0xBE` 为固定标记,其后第一个字节解出即为当前模式号。

**W830NB 的 `groupIndex = 3`**,选项(下标顺序经**实机逐个试听确认**):

| 下标 | 含义 |
|---|---|
| 1 | 深度降噪 |
| 2 | 舒适降噪 |
| 3 | 宽频 |
| 4 | 防风噪 |
| 5 | 环境声 |

> 注:这台耳机**没有独立的"关闭"档** —— 环境声(通透)就是关掉降噪后的状态。
> 另外 APK 里 `AncConfigBean` 的 `index` 顺序与实际听感**不完全对应**,下标与名称的绑定以实机试听为准。

## 通道

| 项 | 值 |
|---|---|
| 服务 | `48098501-1a48-11e9-ab14-d663bd873d93` |
| 接收(notify) | `48090001-1a48-11e9-ab14-d663bd873d93` |
| 发送(write) | `48090002-1a48-11e9-ab14-d663bd873d93` |
| 广播名 | `EDIFIER BLE` |

**必须用「无响应写」(ATT Write Command)。** 带响应写(`Write Request`)会被 Windows 以
`ERROR_CANCELLED (0xC7)` 拒绝 —— 该特征要求加密链路,而耳机不与 Windows 完成 LE 绑定。
无响应写可正常送达,且设备会在 `48090001` 上回响应帧。

## 设备发现

程序不依赖固定 MAC:先按配置的地址快速试一次,失败后扫描 BLE 广播 6 秒,
按**广播名含 `EDIFIER`** 或**广播里带协议服务 UUID(48098501)** 匹配,逐个尝试连接。

## 其它命令(来自 APK `CommandIndex`)

| cmd | 含义 |
|---|---|
| `0xD0` | 电量 |
| `0xC6` | 固件版本 |
| `0xC8` | MAC 地址 |
| `0xC9` / `0xCA` | 设备名 查/设 |
| `0x09` | 游戏模式 |
| `0xCD` `0xCE` `0xCF` | 断开 / 关机 / 重配对 |

---

# 六、协议分析过程

三份独立来源交叉印证 + 实机验证:

| 来源 | 作用 |
|---|---|
| `wh201906/mEDIFIER` | 确认 V1 帧格式与整体命令集 |
| `GlitchPunkWTF/edifier-proto` | W820NB 协议文档(`0x2019` 魔数、CRC 算法) |
| **EDIFIER ConneX 1.0.31 APK** | 定出 W830NB 的 **V2 帧**、XOR 0xA5 加密、`[groupIndex, index]` 载荷 |

关键突破点:

1. `CommandCryptUtil.addCRC` 显示 `versionCode <= V1` 才用 `0x2019` 魔数与双字节 CRC,V2 用从 0 起算的单字节校验
2. `CommandManager.getANC()` 证明查询命令是 `0xCC`
3. `CommandPayloadManager.getANC(AncSetRequest)` 揭示了载荷是 `[groupIndex, 选项下标]` 而非 `[模式, 音量]`
4. 用耳机自己推送的帧(`BB EC CC ...`)反推并**逐字节验证**了 V2 帧结构与 XOR 加密

---

# 七、目录

```
edifier_app.py            图形控制台(主程序,pywebview + WebView2)
edifier_anc.py            命令行工具
make_icon.py              生成 ui/icon.ico 与 ui/icon.png
ui/index.html             界面(HTML/CSS/JS)
ui/icon.ico  icon.png     应用图标 / 托盘图标
启动控制台.bat            双击用 pythonw 启动(无控制台窗口)
打包exe.bat               一键打包
requirements.txt
```

---

# 免责声明

## 与厂商无关

本项目是**独立的第三方工具**,与漫步者(EDIFIER / 深圳市漫步者科技股份有限公司)及其关联公司
**没有任何关系**,未获其授权、认可或背书,也不代表其立场。

"EDIFIER"、"W830NB" 等名称与商标归其各自权利人所有。本项目仅在**说明兼容的设备型号**这一必要范围内
使用这些名称(指名性使用),不使用其商标、Logo 或官方素材作为本项目的标识。

## 不包含第三方资源

本仓库只包含独立编写的源代码与本项目自行绘制的图标。**不包含、不分发**任何第三方的程序、
反编译产物、固件、图片、字体或其他资源文件。协议分析过程中产生的中间文件不纳入版本控制。

## 用途限制

本项目的目的是让用户能操作**自己拥有**的设备(互操作性),不涉及规避任何技术保护措施,
也不用于绕过付费、订阅或访问控制。请在所在国家/地区法律允许的范围内使用;
因使用本软件产生的任何后果由使用者自行承担,作者不提供任何明示或默示的担保。
