# Mayzing Session Bridge

Phiên bản **1.1.0** bổ sung đọc danh sách sản phẩm và đặt sản phẩm mặc định của campaign.
Nếu đã cài bản cũ, nạp lại tiện ích từ thư mục cập nhật rồi ghép lại với tool.

Tiện ích nối một tab Mayzing đã đăng nhập với tool trên máy. Không cần Playwright,
không bật cổng debugger và không sao chép cookie ra khỏi Chrome.

## Cài lần đầu

1. Trong đúng hồ sơ Chrome bạn dùng cho shop, mở `chrome://extensions`.
2. Bật **Developer mode**, chọn **Load unpacked**, chọn thư mục `chrome-extension` chứa file này.
3. Mở Mayzing và đăng nhập thủ công đến khi thấy dashboard shop.

## Kết nối và sử dụng

1. Trong tool `http://127.0.0.1:8780`, tab Mayzing, bấm **Tạo mã kết nối**.
2. Quay lại tab Mayzing đã đăng nhập. Bấm tiện ích **Mayzing Session Bridge** trên thanh tiện ích.
3. Dán mã, bấm **Cho phép tool dùng tab Mayzing này**.
4. Quay lại tool, bấm **Bật tự động upload**. Tool kiểm tra phiên bằng danh sách store trước.
5. Chọn store/origin và sử dụng Prep → Duyệt → Batch & upload như bình thường.

Mã kết nối có hạn **24 giờ từ lúc tạo**, chỉ dùng **một lần**. Khi đã ghép thành công,
kết nối không bị giới hạn bởi thời hạn mã. Mã và phiên kết nối được giữ trong bộ nhớ;
sau khi khởi động lại tool, tạo mã mới và ghép lại Chrome.

**Tắt tự động** dừng gửi lệnh mới. **Ngắt kết nối** trong tiện ích thu hồi kết nối với tool.
Chrome vẫn mở bình thường. Khi đóng Chrome hoặc khởi động lại tool, ghép lại để dùng tiếp.
Nếu mất kết nối trong lúc gửi thao tác, kiểm tra Task log Mayzing trước khi chạy lại.

## Quyền và dữ liệu

- `activeTab` và `scripting`: chỉ thao tác trên tab Mayzing bạn chủ động chọn khi bấm tiện ích.
- `storage`: lưu thông tin kết nối trong bộ nhớ phiên Chrome, không lưu cookie.
- Host access chỉ dành cho `127.0.0.1` / `localhost`, nơi tool đang chạy.
- Không yêu cầu quyền `cookies`, `debugger`, history, mật khẩu hay truy cập mọi website.
- Chỉ gửi các thao tác Mayzing được đóng gói sẵn tới `/api/graphql` trên chính tab Mayzing.
- Chọn sản phẩm mặc định dùng `UpdateStoreGroupProductOrder`, giữ đủ các sản phẩm còn lại trong campaign.
- Khi gặp trang xác minh, tiện ích dừng; bạn phải tự hoàn tất bước trên trang.

Nguồn và toàn bộ mã có sẵn trong thư mục này để kiểm tra trước khi cài.
