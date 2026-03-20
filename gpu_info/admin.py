from django.contrib import admin
from django.db.models import CharField, F, Value
from django.db.models.functions import Cast, Coalesce, Concat, NullIf, Trim
from django.utils.html import format_html
from django.utils import timezone

from .models import GPUServer, GPUInfo


class GPUInfoInline(admin.TabularInline):
    model = GPUInfo
    fields = ('index', 'name', 'utilization', 'memory_usage', 'usernames', 'complete_free', 'free_since', 'update_at')
    readonly_fields = ('index', 'name', 'utilization', 'memory_usage', 'usernames', 'complete_free', 'free_since', 'update_at')

    show_change_link = True

    def usernames(self, obj):
        return obj.usernames()

    def memory_usage(self, obj):
        memory_total = obj.memory_total
        memory_used = obj.memory_used
        return '{:d} / {:d} MB ({:.0f}%)'.format(memory_used, memory_total, memory_used / memory_total * 100)

    memory_usage.short_description = '显存占用率'
    usernames.short_description = '使用者'

    def get_extra(self, request, obj, **kwargs):
        return 0

    def has_add_permission(self, request, obj):
        return False

    def has_change_permission(self, request, obj):
        return False

    def has_delete_permission(self, request, obj):
        return False


@admin.register(GPUServer)
class GPUServerAdmin(admin.ModelAdmin):
    fields = ('alias', 'ip', 'hostname', 'port', 'valid', 'can_use')
    list_display = ('display_name', 'ip', 'hostname', 'port', 'valid', 'can_use')
    list_editable = ('can_use',)
    search_fields = ('alias', 'ip', 'hostname')
    list_display_links = ('display_name',)
    inlines = (GPUInfoInline,)
    ordering = ('ip',)
    readonly_fields = ('hostname',)

    class Media:
        # custom css
        css = {
            'all': ('css/admin/custom.css', )
        }

    def has_add_permission(self, request):
        return request.user.is_superuser

    def display_name(self, obj):
        return obj.display_name

    display_name.short_description = '显示名称'


@admin.register(GPUInfo)
class GPUInfoAdmin(admin.ModelAdmin):
    list_display = ('server_display_name', 'gpu_index', 'utilization', 'memory_usage', 'usernames', 'complete_free', 'update_since', 'free_since_duration')
    list_filter = ('server', 'name', 'complete_free')
    search_fields = ('uuid', 'name', 'memory_used', 'server__alias', 'server__hostname', 'server__ip')
    list_display_links = ('server_display_name',)
    readonly_fields = ('uuid', 'name', 'index', 'utilization', 'memory_total', 'memory_used','server', 'processes', 'use_by_self', 'complete_free', 'free_since', 'update_at')

    class Media:
        css = {
            'all': ('css/admin/custom.css', )
        }
        js = ('js/admin/gpu_info_relative_time.js',)

    def get_queryset(self, request):
        qs = self.model._default_manager.get_queryset()
        return qs.annotate(server_display_name_order=self.get_server_display_name_order_expr())

    def get_ordering(self, request):
        return ('server_display_name_order', 'index')

    def get_server_display_name_order_expr(self):
        return Coalesce(
            NullIf(Trim(F('server__alias')), Value('')),
            NullIf(Trim(F('server__hostname')), Value('')),
            Concat(
                F('server__ip'),
                Value(':'),
                Cast(F('server__port'), output_field=CharField()),
            ),
        )

    def usernames(self, obj):
        return format_html('<span class="gpuinfo-cell gpuinfo-usernames">{}</span>', obj.usernames())

    def has_add_permission(self, request):
        return False

    def server_display_name(self, obj):
        return format_html('<span class="gpuinfo-cell gpuinfo-server">{}</span>', obj.server.display_name)

    def gpu_index(self, obj):
        return obj.index

    def memory_usage(self, obj):
        memory_total = obj.memory_total
        memory_used = obj.memory_used
        value = '{:d} / {:d} MB ({:.0f}%)'.format(memory_used, memory_total, memory_used / memory_total * 100)
        return format_html('<span class="gpuinfo-cell gpuinfo-memory">{}</span>', value)

    def update_since(self, obj):
        epoch_ms = int(obj.update_at.timestamp() * 1000)
        absolute_time = obj.update_at.strftime('%Y-%m-%d %H:%M:%S')
        return format_html(
            '<time class="gpuinfo-relative-time" data-epoch-ms="{}" data-relative-mode="ago" title="{}">{}</time>',
            epoch_ms,
            absolute_time,
            self._relative_time_text(obj.update_at, suffix='前'),
        )

    def free_since_duration(self, obj):
        if not obj.complete_free or obj.free_since is None:
            return '-'

        epoch_ms = int(obj.free_since.timestamp() * 1000)
        absolute_time = obj.free_since.strftime('%Y-%m-%d %H:%M:%S')
        return format_html(
            '<time class="gpuinfo-relative-time gpuinfo-idle-duration" data-epoch-ms="{}" data-relative-mode="duration" title="{}">{}</time>',
            epoch_ms,
            absolute_time,
            self._relative_time_text(obj.free_since, suffix=''),
        )

    def _relative_time_text(self, dt, suffix='前'):
        seconds = max(int((timezone.now() - dt).total_seconds()), 0)
        if seconds < 10:
            return '刚刚'
        if seconds < 60:
            return f'{seconds}秒{suffix}'

        minutes = seconds // 60
        if minutes < 60:
            return f'{minutes}分钟{suffix}'

        hours = minutes // 60
        if hours < 24:
            return f'{hours}小时{suffix}'

        days = hours // 24
        if days < 30:
            return f'{days}天{suffix}'

        months = days // 30
        if months < 12:
            return f'{months}个月{suffix}'

        years = days // 365
        return f'{years}年{suffix}'

    server_display_name.short_description = '服务器'
    server_display_name.admin_order_field = 'server_display_name_order'
    gpu_index.short_description = 'GPU序号'
    gpu_index.admin_order_field = 'index'
    memory_usage.short_description = '显存占用率'
    update_since.short_description = '更新时间'
    update_since.admin_order_field = 'update_at'
    free_since_duration.short_description = '已空闲时间'
    free_since_duration.admin_order_field = 'free_since'
    usernames.short_description = '使用者'
