import traceback

from django.core.mail import send_mail

from gpu_tasker.settings import EMAIL_NOTIFICATION


TASK_START_NOTIFICATION_TITLE = '任务开始运行'
TASK_START_NOTIFICATION_TEMPLATE = \
'''任务[{}]开始运行
任务运行详情：
任务名称：{}
工作目录：{}
命令：
----------
{}
----------
服务器：{}
显卡：{}
开始时间：{}
'''

TASK_FINISH_NOTIFICATION_TITLE = '任务运行完成'
TASK_FINISH_NOTIFICATION_TEMPLATE = \
'''任务[{}]运行完成
任务运行详情：
任务名称：{}
工作目录：{}
命令：
----------
{}
----------
服务器：{}
显卡：{}
结束时间：{}

请登录GPUTasker查看运行结果
'''


TASK_FAIL_NOTIFICATION_TITLE = '任务运行失败'
TASK_FAIL_NOTIFICATION_TEMPLATE = \
'''任务[{}]运行失败
任务运行详情：
任务名称：{}
工作目录：{}
命令：
----------
{}
----------
服务器：{}
显卡：{}
结束时间：{}

失败摘要：{}
远端退出码：{}
最后输出：{}

请登录GPUTasker查看错误信息
'''


def get_server_display_name(server):
    alias = (server.alias or '').strip()
    if alias:
        return alias

    hostname = (server.hostname or '').strip()
    if hostname:
        return hostname

    return server.ip


def send_email(address, title, content):
    if EMAIL_NOTIFICATION:
        try:
            from gpu_tasker.settings import DEFAULT_FROM_EMAIL
            send_mail(title, content, DEFAULT_FROM_EMAIL, [address], fail_silently=False)
        except Exception:
            es = traceback.format_exc()
            print('Send email fail')
            print(es)


def check_email_config(func):
    def wrapper(*args, **kw):
        if EMAIL_NOTIFICATION:
            running_log = args[0]
            address = running_log.task.user.email
            if address is not None and address != '':
                return func(*args, **kw)
    return wrapper


@check_email_config
def send_task_start_email(running_log):
    address = running_log.task.user.email
    title = TASK_START_NOTIFICATION_TITLE
    server_display_name = get_server_display_name(running_log.server)
    content = TASK_START_NOTIFICATION_TEMPLATE.format(
        running_log.task.name,
        running_log.task.name,
        running_log.task.workspace,
        running_log.task.cmd,
        server_display_name,
        running_log.gpus,
        running_log.start_at.strftime("%Y-%m-%d %H:%M:%S")
    )
    send_email(address, title, content)


@check_email_config
def send_task_finish_email(running_log):
    address = running_log.task.user.email
    title = TASK_FINISH_NOTIFICATION_TITLE
    server_display_name = get_server_display_name(running_log.server)
    content = TASK_FINISH_NOTIFICATION_TEMPLATE.format(
        running_log.task.name,
        running_log.task.name,
        running_log.task.workspace,
        running_log.task.cmd,
        server_display_name,
        running_log.gpus,
        running_log.update_at.strftime("%Y-%m-%d %H:%M:%S")
    )
    send_email(address, title, content)


@check_email_config
def send_task_fail_email(running_log):
    address = running_log.task.user.email
    title = TASK_FAIL_NOTIFICATION_TITLE
    server_display_name = get_server_display_name(running_log.server)
    diagnostics = running_log.get_failure_diagnostics()
    content = TASK_FAIL_NOTIFICATION_TEMPLATE.format(
        running_log.task.name,
        running_log.task.name,
        running_log.task.workspace,
        running_log.task.cmd,
        server_display_name,
        running_log.gpus,
        running_log.update_at.strftime("%Y-%m-%d %H:%M:%S"),
        diagnostics.get('failure_summary') or '运行失败',
        diagnostics.get('remote_exit_code') or '-',
        diagnostics.get('last_output') or '-'
    )
    send_email(address, title, content)
