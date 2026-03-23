# GPU Tasker

轻量好用的 GPU 机群任务调度工具。

[![simpleui](https://img.shields.io/badge/developing%20with-Simpleui-2077ff.svg)](https://github.com/newpanjing/simpleui)
[![docker build](https://github.com/cnstark/gputasker/actions/workflows/docker-build.yml/badge.svg)](https://hub.docker.com/r/cnstark/gputasker)

## 介绍

GPU Tasker 用于在单机或多机 GPU 环境中按条件调度任务。当前项目的运行方式比较直接：

- Web 管理界面是 Django Admin。
- 调度器是单独运行的 `main.py`。
- Master 会通过 SSH 连接各个 Node，执行 `hostname`、`nvidia-smi`、`ps` 等命令收集状态。
- 任务满足条件后，Master 会继续通过 SSH 在目标 Node 上执行任务命令。

这意味着：

- 任务的 `工作目录` 必须是 Node 上的路径，不是 Master 本机路径。
- 任务的 `命令` 也是在 Node 上执行，不是在 Master 上执行。
- 训练代码和数据需要提前放到 Node 上，或者放到所有 Node 都能访问的共享目录。

当前版本适合需要快速搭一个轻量级调度器的场景，但还不适合复杂的多租户资源管理平台。

## 环境要求

### Master

- Linux 或 Windows
- Python 3.8 到 3.10
- 可用的 SSH 客户端
- `django`
- `django-simpleui`

### Node

- OpenSSH Server
- `bash`
- `nvidia-smi`
- `ps`
- `awk`
- `grep`

建议：

- 所有 Node 使用同一个 Linux 用户名，例如 `gpuuser`
- 所有 Node 挂载同一个共享目录，保证训练代码路径一致

## 部署方式

GPU Tasker 支持 Linux Master 手动部署、Linux Master Docker 部署，以及 Windows Master + Linux Node 的混合部署。

### Linux Master 手动部署

1. 克隆项目

```bash
git clone https://github.com/cnstark/gputasker.git
cd gputasker
```

2. 安装依赖

```bash
python -m pip install django django-simpleui
```

3. 编辑 `gpu_tasker/settings.py`，按实际情况调整数据库等 Django 基本设置。

- 单用户或小规模使用时，直接用 SQLite 即可。
- 如果需要容器化部署或更稳定的数据库，再切 MySQL。

4. 初始化数据库

```bash
python manage.py makemigrations
python manage.py migrate
```

5. 创建管理员

```bash
python manage.py createsuperuser
```

6. 启动 Web 管理界面

```bash
python manage.py runserver --insecure 0.0.0.0:8888
```

7. 启动调度主进程

```bash
python main.py
```

访问 `http://your_server:8888/admin` 进入后台。

### Linux Master Docker 部署

这条路径更适合 Linux Master，不建议直接照搬到 Windows Master。当前仓库里的 `docker-compose.yml` 默认挂载了 Linux 主机路径 `/etc/localtime` 和 `/etc/timezone`。

1. 安装 [Docker](https://docs.docker.com/get-docker/) 和 [docker compose](https://docs.docker.com/compose/install/)

2. 克隆项目

```bash
git clone https://github.com/cnstark/gputasker.git
cd gputasker
```

3. 启动服务

```bash
sudo docker-compose up -d
```

4. 等待初始化完成后创建管理员

当 `http://your_server:8888/admin` 可以访问时执行：

```bash
sudo docker exec -it gputasker_django python manage.py createsuperuser
```

### Windows Master + Linux Node 部署

这个场景下，推荐在 Windows 上原生运行 Django 和 `main.py`，Linux 服务器只作为被 SSH 控制的执行节点。

#### 1. Windows Master 准备

建议准备以下环境：

- Git
- OpenSSH Client
- Miniconda 或 Anaconda
- 一个可用的 conda 环境

推荐使用不带空格的路径，例如：

```powershell
C:\software\gputasker
```

原因是当前 SSH 命令拼接没有对私钥路径额外加引号，路径中有空格时更容易失败。

如果还没有环境，可以新建：

```powershell
conda create -n gputasker python=3.10 -y
conda activate gputasker
```

安装依赖：

```powershell
conda activate gputasker
cd /d C:\software\gputasker
python -m pip install django django-simpleui
```

可选检查：

```powershell
python manage.py check
```

#### 2. Linux Node 准备

每台 Node 至少需要：

- `bash`
- `nvidia-smi`
- `ps`
- `awk`
- `grep`

同时，Master 必须能够免密 SSH 登录所有 Node。推荐在 Windows Master 上生成一对专用密钥：

```powershell
ssh-keygen -t ed25519 -f C:\software\gputasker\master_ed25519
```

然后把公钥追加到每台 Node 的 `~/.ssh/authorized_keys`。

#### 3. 先验证 SSH 和 GPU 查询

在 Windows Master 上先手动验证：

```powershell
ssh -i C:\software\gputasker\master_ed25519 gpuuser@192.168.1.101 "hostname"
ssh -i C:\software\gputasker\master_ed25519 gpuuser@192.168.1.101 "nvidia-smi --query-gpu=uuid,gpu_name,utilization.gpu,memory.total,memory.used --format=csv"
```

如果这两条命令都能正常返回，再继续部署 GPU Tasker。

#### 4. 初始化并启动 Master

以下步骤都在 Windows Master 上执行，且工作目录必须是仓库根目录。

初始化数据库：

```powershell
conda activate gputasker
cd /d C:\software\gputasker
python manage.py makemigrations
python manage.py migrate
```

创建管理员：

```powershell
python manage.py createsuperuser
```

启动 Web：

```powershell
python manage.py runserver --insecure 0.0.0.0:8888
```

启动调度器：

```powershell
python main.py
```

访问：

```text
http://127.0.0.1:8888/admin
```

如果需要让局域网其他机器访问，还要同时放行 Windows 防火墙的 `8888` 端口。

#### 5. Windows 常驻运行建议

如果要开机自动运行，建议把 Web 进程和调度进程分别做成 Windows 计划任务或 NSSM 服务。

Web 进程：

```powershell
cd /d C:\software\gputasker
$env:USERPROFILE\miniconda3\envs\gputasker\python.exe manage.py runserver --insecure 0.0.0.0:8888
```

调度进程：

```powershell
cd /d C:\software\gputasker
$env:USERPROFILE\miniconda3\envs\gputasker\python.exe main.py
```

无论使用哪种方式，都要把 `Start in` 设置为仓库根目录，否则 `private_key`、`running_log` 等相对路径可能写到错误位置。

## 后台初始化

访问 `http://your_server:8888/admin` 登录管理后台。

![home](.assets/home.png)

### 1. 配置用户设置

先添加 `用户设置`，填写：

- 服务器用户名，例如 `gpuuser`
- 服务器私钥，把私钥内容完整粘贴进去

保存后，系统会把私钥写入项目目录下的 `private_key` 文件夹。

当前设计下，每个 Django 用户只维护一套：

- `服务器用户名`
- `服务器私钥`

因此同一个 Django 用户提交的任务，默认要求它在所有 Node 上都使用同一个 Linux 用户名。

![user config](.assets/user_config.png)

### 2. 添加 GPU 服务器

进入 `GPU服务器`，逐台添加 Node 的 IP 或域名，以及 SSH 端口。

保存后系统会自动尝试读取：

- 主机名
- GPU 信息

如果节点无法连通，服务器状态会被标记为不可用。

![add server](.assets/add_server.png)

选项说明：

- `是否可用`：服务器当前是否可用。连接失败或无法获取 GPU 状态时会自动变为 `False`。
- `是否可调度`：服务器是否参与调度。手动设为 `False` 后，该服务器不会再被分配任务。

### 3. 添加 GPU 任务

进入 `GPU任务`，填写任务信息并保存。状态为 `准备就绪` 的任务会在服务器满足条件时执行。

![add task](.assets/add_task.png)

关键字段说明：

- `工作目录`：命令执行时所在目录，必须是 Node 上的 Linux 路径。
- `命令`：在 Node 上作为 bash 脚本正文执行，支持多行。
- `GPU数量需求`：任务需要的 GPU 数量。调度器会自动设置 `CUDA_VISIBLE_DEVICES`，任务命令里不要手动设置，也不要写死 `cuda:1` 这类物理卡号。
- `独占显卡`：为 `True` 时，只调度当前没有其他进程占用的 GPU。
- `显存需求`：单张 GPU 需要预留的显存。
- `利用率需求`：单张 GPU 需要满足的空闲利用率。
- `指定服务器`：为空时在所有可调度服务器中寻找资源；不为空时只在指定服务器上等待。
- `状态`：只有 `准备就绪` 的任务会被调度。

任务运行后，可以在 `GPU任务运行记录` 中查看状态和日志。

### 4. conda 任务写法

由于任务是通过 SSH 以远端 bash 脚本的方式执行，直接写：

```bash
conda activate train
```

经常会失败。更稳妥的写法是：

```bash
source /home/gpuuser/miniconda3/bin/activate
conda activate train
python train.py
```

或者直接用解释器绝对路径：

```bash
/home/gpuuser/miniconda3/envs/train/bin/python train.py
```

如果所有 Node 上 conda 安装路径一致，直接写解释器绝对路径通常最省事。

如果训练框架需要设备参数，优先写：

```bash
--device cuda
```

不要写死：

```bash
--device cuda:1
```

因为调度器已经通过 `CUDA_VISIBLE_DEVICES` 把可见卡范围限制好了，任务里再写物理 GPU 编号，容易和实际分配结果冲突。

## 常见问题

### 添加节点后一直不可用

优先检查：

```powershell
ssh -i C:\software\gputasker\master_ed25519 gpuuser@<node_ip> "hostname && nvidia-smi"
```

如果这里失败，GPU Tasker 也一定会失败。

### 任务一直不启动

优先确认：

- `main.py` 是否真的在运行
- 任务状态是否是 `准备就绪`
- 节点是否 `是否可用 = True`
- 节点是否 `是否可调度 = True`
- GPU 数量、显存、利用率条件是否设置过严

### 工作目录填了 Windows 路径

这是错误用法。任务是在 Linux Node 上执行，工作目录必须填写 Linux 路径，例如：

```text
/data/share/project_a
```

不能写成：

```text
C:\project_a
```

### 不同 Node 的代码路径不一致

同一个任务可能会在某些节点上运行成功，在另一些节点上失败。解决方式只有两类：

1. 使用共享存储，让所有 Node 保持同一路径。
2. 每个任务显式指定固定服务器，不做全局调度。

## 通知设置

GPU Tasker 支持邮件通知。任务开始和结束时，可以向用户发送邮件提醒。

### 开启邮箱 SMTP

进入邮箱后台，开启 SMTP 功能并获取 SMTP 密钥。不同邮件服务商的配置方式不同，具体以对应服务商文档为准。

### 配置邮件通知

复制 `gpu_tasker/email_settings_sample.py` 为 `gpu_tasker/email_settings.py`。

```bash
cd gpu_tasker
cp email_settings_sample.py email_settings.py
```

编辑 `email_settings.py`：

```python
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.163.com'
EMAIL_PORT = 465
EMAIL_HOST_USER = 'xxx@163.com'
EMAIL_HOST_PASSWORD = 'xxx'
EMAIL_USE_SSL = True
EMAIL_USE_LOCALTIME = True
DEFAULT_FROM_EMAIL = 'GPUTasker<{}>'.format(EMAIL_HOST_USER)
SERVER_EMAIL = EMAIL_HOST_USER
```

收信邮箱对应 Django 用户的 `电子邮件地址` 字段，可在后台设置。

![user email](.assets/user_email.png)

## 更新 GPU Tasker

更新后请务必同步数据表，并重新启动 `main.py`。

```bash
git pull
python manage.py makemigrations
python manage.py migrate
python main.py
```

如果 `main.py` 已在运行，先结束旧进程，再启动新进程。

## 致谢

本仓库基于原作者 [cnstark](https://github.com/cnstark) 的 [gputasker](https://github.com/cnstark/gputasker) fork 而来。

感谢 [simpleui](https://github.com/newpanjing/simpleui) 团队提供的优秀工具。
