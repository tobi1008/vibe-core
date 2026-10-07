#!/bin/bash
echo "Bắt đầu cập nhật Vibe Hosting API..."
cd /opt/vibe-hosting

# Xóa các thay đổi sửa tay lặt vặt trên VPS để ưu tiên code từ GitHub
git stash
git pull origin main

# Kích hoạt môi trường và cập nhật thư viện (nếu có)
source venv/bin/activate
pip install -r requirements.txt

# Khởi động lại Backend
systemctl restart vibe-api.service
echo "Cập nhật và khởi động lại thành công!"
