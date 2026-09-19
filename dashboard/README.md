# 本地看板

```powershell
python dashboard\server.py
```

浏览器打开 `http://127.0.0.1:8787`。启动时终端会打印 token；首次执行写操作时输入 token。

## 终端实时输出

从看板启动三个服务后，服务子进程的 stdout/stderr 会同时：

1. 在运行 `dashboard/server.py` 的服务器终端实时显示；
2. 写入 `logs/dashboard_collect.log`、`logs/dashboard_process.log`、`logs/dashboard_distribute.log`；
3. 保留在服务日志文件中，可供看板日志页读取。

终端前缀：

```text
[采集] ...
[处理] ...
[分发] ...
```

看板托管的 `collect`、`process` 服务固定使用项目根的 `.venv\Scripts\python.exe`，不会使用系统全局 Python。启动日志会显示实际解释器路径；如果 `.venv` 不存在，服务会直接失败并提示先创建项目环境。

- 实时待分发队列
- 运行状态和配置状态
- 立即采集、运行处理、运行自检
- 深色界面、响应式布局、减少动画模式
- SSE 实时刷新

默认只监听本机。不要把服务直接暴露到公网；远程访问需要额外的反向代理、认证和 HTTPS。
