# 插件

灵桥可以装自己的插件：在这个文件夹里放一个子文件夹，里面有 `plugin.json` 和一个 Python 模块，重新打开灵桥，侧栏就会多出一项。插件可以是另一个 Git 仓库（公开或私有都行），「检查更新」会连它一起更新。

插件和灵桥跑在同一个进程里，权限和灵桥一样，**只装自己信得过的**。某个插件装坏了，侧栏把它显示成灰色并写明原因，灵桥其他功能照常。

## 目录

```
plugins/
  my-plugin/
    plugin.json
    plugin.py        入口，要有 create(env, folder)
    page.js          页面脚本（可选）
    page.css         页面样式（可选）
    tests/           测试（可选；检查更新时会跑）
```

## plugin.json

```json
{
  "id": "my-plugin",
  "name": "我的插件",
  "version": "1.0.0",
  "min_core": "3.5.0",
  "backend": "plugin.py",
  "assets": ["page.js", "page.css"],
  "frontend": {"global": "MyPluginPage", "script": "page.js", "style": "page.css"},
  "nav": {"section": "插件", "label": "我的插件", "symbol": "◇", "title": "鼠标停在侧栏上时的说明"}
}
```

- `id`：小写字母开头，字母、数字、`-`，2–32 位。它同时是侧栏分组名和接口前缀 `/api/<id>/`，不能和灵桥自己的名字重复（`usage`、`trash`、`accounts`、`transfer`、`update` 等）。
- `assets`：页面要用的文件，只能是插件文件夹里的 `.js` / `.css`。灵桥只提供这里登记过的文件，读取时要带 token 头。
- `min_core`：需要的最低灵桥版本，灵桥太旧时不加载并说明原因。

## 后端

```python
class MyPlugin:
    def __init__(self, env, folder):
        self.env = env          # 灵桥的 server 模块：HOME、BRIDGE、CONFIG、log() 等
        self.folder = folder

    def handle_get(self, path, query):      # path 以 /api/<id>/ 开头
        return {'ok': True}                 # 也可以返回 ('file', bytes, content_type)

    def handle_post(self, path, body):      # body 是 JSON 对象
        return {'ok': True}

    def status(self):                       # 可选：不可用时侧栏变灰
        return {'available': True, 'reason': ''}

    def busy_reason(self):                  # 可选：正在忙就返回原因，检查更新会等它
        return ''

    def shutdown(self):                     # 可选：灵桥退出时调用
        pass


def create(env, folder):
    return MyPlugin(env, folder)
```

需要用户再确认一次的操作：抛出带 `confirm` 属性的异常（比如 `ValueError`），接口会回 409，页面可以弹确认再重发。

插件文件夹里其他 `.py` 文件的名字不能和灵桥或 Python 自带的模块重名（比如 `json.py`、`server.py`），否则不加载。

## 页面

页面脚本在 `window.<global>` 上挂一个对象：`show(host)` 把页面画进 `host`，`hide()`、`refresh()` 可选。请求接口时带上 `X-Bridge-Token: window.BRIDGE_TOKEN` 请求头。

## 测试

插件的 `tests/` 用标准 unittest。灵桥跑插件测试时会设置环境变量 `LINGQIAO_CORE` 指向灵桥文件夹，测试里用它找到 `app/` 和 `.bridge/`。
