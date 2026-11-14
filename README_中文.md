# 投资助手 · 云端版（GitHub Pages + 每日自动更新）

**这是什么：** 一个放在网上的投资小网页。每天北京时间早上 9:05 左右，GitHub 的服务器会自动帮你：
拉取自选股收盘价 → 让 AI 联网写一份中文「每日雷达」（隔夜美股、美联储/宏观、自选相关消息、3～5 只异动股）→ 更新网页。
你的电脑关机也照样更新，手机和电脑打开同一个网址就能看。

> ⚠️ **先看隐私提醒**
> - GitHub Pages **是公开网页**，知道网址的人都能打开。免费账号做 Pages，仓库也必须是**公开**的。
> - 所以云端只放：自选代码、价格、AI 写的新闻。**不放**你的持仓数量、成本、金额。
> - 你的持仓只存在**你自己浏览器**里（localStorage）。网页会在你本机把云端价格和你的持仓合起来算盈亏、算止损预警。
> - 代价：电脑和手机的持仓要**各录一遍**（或用「设置 → 导出/导入」同步）。换浏览器、清缓存会丢，记得导出备份。
> - API 密钥只放 GitHub 的 Secrets，**千万不要**写进文件、也不要发给别人。
>
> 内容仅供学习与记录参考，**不构成投资建议**。AI 联网整理的内容可能有错，「异动股」只是线索，不是买入理由。

---

## 一、一次性设置（大约 15 分钟，全程网页操作，电脑上做最方便）

### 第 1 步：注册 GitHub
1. 打开 <https://github.com>，点 Sign up，用邮箱注册，按提示验证邮箱。
2. 记住你的**用户名**（后面网址里要用）。国内打开如果很慢，可能需要开 VPN。

### 第 2 步：新建仓库
1. 登录后点右上角 **＋ → New repository**。
2. Repository name 随便起，比如 `invest`。
3. 选 **Public**（公开）。
4. 勾选 **Add a README file**（这样仓库不是空的，后面更好操作）。
5. 点绿色的 **Create repository**。

### 第 3 步：上传文件
1. 把我发你的压缩包 `invest-cloud.zip` **先解压**到桌面（GitHub 不能直接传 zip）。
2. 在你的仓库页面点 **Add file → Upload files**。
3. 打开解压后的文件夹，**全选里面的所有东西**（`index.html`、`config.json`、`data.json`、`requirements.txt`、`README_中文.md`、`scripts` 文件夹、`.github` 文件夹），拖到网页的上传区域。
4. 等文件都显示出来，拉到最下面点绿色的 **Commit changes**。

> 如果拖完以后发现没有 `.github` 文件夹（有些浏览器会漏掉以点开头的文件夹），就用这个办法补上：
> 仓库页面点 **Add file → Create new file**，在文件名那一栏**直接输入** `.github/workflows/daily.yml`（输入斜杠会自动变成文件夹），
> 然后把解压文件夹里 `.github/workflows/daily.yml` 的全部内容复制粘贴进去，点 **Commit changes**。

### 第 4 步：填 API 密钥（Secrets）
1. 仓库页面点上方的 **Settings**（设置）。
2. 左边找到 **Secrets and variables → Actions**。
3. 点 **New repository secret**：
   - Name 填 `GEMINI_API_KEY`
   - Secret 粘贴你的 Gemini 密钥（获取方法见下面第三节）
   - 点 **Add secret**。

### 第 5 步：允许自动更新写回仓库
1. Settings 左边 **Actions → General**。
2. 拉到最下面 **Workflow permissions**，选 **Read and write permissions**，点 **Save**。

### 第 6 步：开启网页（Pages）
1. Settings 左边点 **Pages**。
2. **Source** 选 **Deploy from a branch**。
3. **Branch** 选 `main`，文件夹选 `/ (root)`，点 **Save**。
4. 等 1～2 分钟，刷新这个页面，上面会出现你的网址，格式是：
   `https://你的用户名.github.io/仓库名/`

### 第 7 步：手动跑一次，看看有没有成功
1. 仓库上方点 **Actions**。（如果提示 “I understand my workflows, go ahead and enable them”，点它。）
2. 左边点 **每日雷达**，右边点 **Run workflow → Run workflow**。
3. 等 1～3 分钟，出现绿色 ✅ 就成功了。红色 ❌ 就点进去看报错（常见原因见最后的“出问题怎么办”）。
4. 回到你的网址，刷新页面，「雷达」页应该就有内容了。

### 第 8 步：加书签
- **电脑：** 浏览器里按 Ctrl+D 收藏网址。
- **手机：** 用浏览器打开网址 → 菜单里选“添加到主屏幕”，以后像 App 一样点开。
- 第一次打开后，到「持仓」页录入你的持仓，到「设置 → 止损预警」设阈值。

---

## 二、日常使用
- 每天早上打开网址，先看「雷达」页：隔夜美股总结、自选相关消息、异动股、**止损预警**（红色提示）。
- 「雷达」页的按钮「＋ 把云端自选加入我的自选」可以把云端清单一键加到本机自选。
- 原来的功能都还在：自选、持仓盈亏、财经日历、多家 AI 对话（要在「设置」里填各家密钥，只存在你本机浏览器）。
- 云端数据没更新、或者网址打不开时，页面会给出提示；自选/持仓/日历/AI 对话照常能用。
- 只有周二到周六早上会自动更新（对应美股周一到周五的收盘）。美股休市日，价格会和前一天一样。

## 三、怎么拿 AI 密钥
### Gemini（默认，推荐先用这个）
1. 打开 <https://aistudio.google.com/apikey>，用 Google 账号登录。
2. 点 **Create API key**，复制出来的那串字符，粘贴到上面第 4 步的 Secret 里。
3. **在中国大陆打开 Google AI Studio 可能需要开 VPN。** 但每天的自动任务是在 GitHub 国外的服务器上跑的，不受你网络影响，所以只有“申请密钥”那一步需要。
4. 免费额度和“联网搜索”是否免费，以 [Gemini 价格页](https://aistudio.google.com/docs/pricing) 为准，用之前请先确认。

### 改用 Grok（可选）
1. 去 <https://console.x.ai> 创建 API 密钥，并在 Billing 里充一点钱（Grok 没有免费额度）。
2. 在 GitHub 的 Secrets 里再添加两个：
   - `XAI_API_KEY` = 你的 xAI 密钥
   - `AI_PROVIDER` = `grok`
3. 想换回 Gemini：把 `AI_PROVIDER` 改成 `gemini`，或者直接删掉它。
4. 模型默认 `gemini-3.8-flash` / `grok-4.3`。想换模型，加一个 Secret：`GEMINI_MODEL` 或 `XAI_MODEL`，填模型名。模型名会更新，如果报“模型不存在”，就去各家官网看现在的名字。

## 四、怎么改自选股
1. 仓库里点开 `config.json`，点右上角的铅笔图标（Edit）。
2. 改 `"watchlist"` 那一行，比如 `["SPYM", "QQQM", "TQQQ", "SOXL", "NVDA"]`，代码用英文大写，美股/ETF 直接写代码。
3. 想显示中文名，改 `"names"` 里对应的名字（可选）。
4. 点 **Commit changes**。下次自动更新，或者手动 Run workflow 一次就生效。
5. 港股写成 `0700.HK`，加密货币写成 `BTC-USD`（这两种我只做了基本支持，美股最稳）。
6. **config.json 是公开的**，只写代码，别写数量和金额。

## 五、可选：微信推送（Server酱）
1. 打开 <https://sct.ftqq.com>，用微信登录，拿到 SendKey。
2. 在 Secrets 里添加 `SERVERCHAN_KEY` = 你的 SendKey。
3. 每天更新完会推一条简短摘要（自选涨跌幅 + 头条 + 异动代码），**不含任何金额和持仓**。不填就不推送。

## 六、要花多少钱
- GitHub：免费。
- Gemini：免费额度通常够这种“每天一次”的用量，大概率 0 元，但额度政策会变，以官网为准。
- Grok：没有免费额度，每次运行要花一点点钱（模型调用 + 联网搜索，具体见 [xAI 价格页](https://docs.x.ai/developers/pricing)）。每天一次的话，预计一年几十元量级，实际以官网价格为准。

## 七、已知限制
- **定时会晚：** GitHub 的定时任务经常晚几分钟到几十分钟才开始跑，高峰期更久。
- **行情接口不是官方的：** 价格来自 yfinance（抓取雅虎财经），偶尔会失败。失败时页面会保留上一次的价格并标“过期”。
- **AI 可能出错或失败：** 失败时只更新价格，页面会写明原因，并显示上一次的雷达内容（标“旧内容”）。
- **github.io 在国内有时打不开或很慢**，手机不开 VPN 可能看不到。
- **仓库长期没动静时 GitHub 可能暂停定时任务**（一般每天有更新提交就不会）。如果页面提示“很多天没更新”，去 Actions 页面看看，必要时点一下启用。
- **提醒只在你打开网页时计算**，不会主动推送到手机（想要推送请用上面的 Server酱）。
- **持仓不跨设备同步**（这是为了隐私）。

## 八、出问题怎么办
| 现象 | 怎么办 |
|---|---|
| Actions 红色 ❌，提示 `Permission ... denied` 或 push 失败 | 回第 5 步，把 Workflow permissions 改成 Read and write |
| 页面显示“示例占位数据” | 还没成功跑过。去 Actions 手动 Run workflow |
| 页面说“AI 雷达没有生成成功：没有设置 GEMINI_API_KEY” | 检查 Secrets 名字是不是一字不差：`GEMINI_API_KEY` |
| 提示 API key not valid | 密钥复制错了，重新生成一个，更新 Secret |
| 提示模型不存在 | 用 `GEMINI_MODEL` / `XAI_MODEL` 这个 Secret 指定一个现在有效的模型名 |
| 网址打开是 404 | Pages 还没生效，等几分钟；或检查第 6 步的设置 |
| 价格全是空的 | 多半是雅虎接口临时故障，过一会儿再手动跑一次 |

## 九、文件说明（不用看，想了解再看）
- `index.html`：网页本体。
- `config.json`：自选股清单（公开）。
- `data.json`：每天自动生成的数据（公开）。仓库里一开始是**示例占位**，没有真实价格。
- `scripts/update_daily.py`：每天跑的脚本。
- `.github/workflows/daily.yml`：定时任务设置（周二到周六 UTC 01:05 = 北京时间 09:05，也可手动运行）。
- `requirements.txt`：脚本要装的库。
