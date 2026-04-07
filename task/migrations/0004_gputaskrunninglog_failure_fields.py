from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('task', '0003_gputaskrunninglog_stop_requested'),
    ]

    operations = [
        migrations.AddField(
            model_name='gputaskrunninglog',
            name='failure_hint',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='失败提示'),
        ),
        migrations.AddField(
            model_name='gputaskrunninglog',
            name='failure_summary',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='失败摘要'),
        ),
        migrations.AddField(
            model_name='gputaskrunninglog',
            name='last_output',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='最后输出'),
        ),
        migrations.AddField(
            model_name='gputaskrunninglog',
            name='remote_exit_code',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='远端退出码'),
        ),
    ]
