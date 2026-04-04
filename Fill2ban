#!/bin/bash

# ====================================================
# 方案一：Fail2ban 极致安全加固脚本 (修正版)
# ====================================================

echo "--- 正在开始配置 Fail2ban 安全脚本 ---"
read -p "请输入你的 Telegram Bot Token: " TG_TOKEN
read -p "请输入你的 Telegram Chat ID: " TG_CHATID
read -p "请输入需要加白的 IP (多个用空格隔开): " WHITELIST_IPS

# 先解一下 apt 锁
sudo fuser -vki /var/lib/dpkg/lock-frontend || true
sudo dpkg --configure -a

# 安装依赖
sudo apt-get update && sudo apt-get install -y fail2ban curl

# 获取主机名
MY_HOSTNAME=$(hostname)

# 创建 TG 告警动作脚本 (注意 %%0A 是为了转义 Fail2ban 的解析)
cat <<EOF | sudo tee /etc/fail2ban/action.d/telegram-notify.conf
[Definition]
actionban = curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \\
            -d "chat_id=${TG_CHATID}" \\
            -d "parse_mode=Markdown" \\
            -d "text=🚨 *SSH 暴力破解拦截*%%0A*主机:* ${MY_HOSTNAME}%%0A*封禁IP:* <ip>%%0A*时长:* 10年%%0A*状态:* 已执行防火墙永久封禁"
actionunban = 
EOF

# 编写核心配置文件
cat <<EOF | sudo tee /etc/fail2ban/jail.local
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 ${WHITELIST_IPS}
bantime  = 315360000
findtime = 600
maxretry = 1

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s
action  = %(action_mwl)s
          telegram-notify
EOF

sudo systemctl restart fail2ban
echo "----------------------------------------"
echo "✅ 修正版部署完成！"
echo "----------------------------------------"
