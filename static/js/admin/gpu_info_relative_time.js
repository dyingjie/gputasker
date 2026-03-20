(function () {
    function formatRelativeTime(epochMs) {
        var seconds = Math.max(0, Math.floor((Date.now() - epochMs) / 1000));
        if (seconds < 10) {
            return '刚刚';
        }
        if (seconds < 60) {
            return seconds + '秒前';
        }

        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) {
            return minutes + '分钟前';
        }

        var hours = Math.floor(minutes / 60);
        if (hours < 24) {
            return hours + '小时前';
        }

        var days = Math.floor(hours / 24);
        if (days < 30) {
            return days + '天前';
        }

        var months = Math.floor(days / 30);
        if (months < 12) {
            return months + '个月前';
        }

        var years = Math.floor(days / 365);
        return years + '年前';
    }

    function updateRelativeTimes() {
        var nodes = document.querySelectorAll('.gpuinfo-relative-time[data-epoch-ms]');
        nodes.forEach(function (node) {
            var epochMs = Number(node.getAttribute('data-epoch-ms'));
            if (!Number.isFinite(epochMs)) {
                return;
            }
            node.textContent = formatRelativeTime(epochMs);
        });
    }

    function initRelativeTimes() {
        updateRelativeTimes();
        window.setInterval(updateRelativeTimes, 10000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initRelativeTimes);
    } else {
        initRelativeTimes();
    }
})();
