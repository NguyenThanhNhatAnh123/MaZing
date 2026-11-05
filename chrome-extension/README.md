# Mayzing Session Bridge

Phiên bản **1.2.0** hỗ trợ MaZing trên HTTPS production và lưu địa chỉ máy chủ đã chọn.
Nếu đã cài bản cũ, nạp lại tiện ích từ thư mục cập nhật rồi ghép lại với tool.

Tiện ích nối một tab Mayzing đã đăng nhập với MaZing trên web hoặc tool local. Không cần Playwright,
không bật cổng debugger và không sao chép cookie ra khỏi Chrome.

## Cài lần đầu

1. Trong đúng hồ sơ Chrome bạn dùng cho shop, mở `chrome://extensions`.
2. Bật **Developer mode**, chọn **Load unpacked**, chọn thư mục `chrome-extension` chứa file này.
3. Mở Mayzing và đăng nhập thủ công đến khi thấy dashboard shop.

## Kết nối và sử dụng

1. Trên mỗi máy / hồ sơ Google Chrome, mở `https://mz.eagleglobal2023.com`, tab Mayzing, bấm **Tạo mã kết nối**. Mỗi hồ sơ trình duyệt có một phiên MaZing và dữ liệu riêng.
2. Quay lại tab Mayzing đã đăng nhập. Bấm tiện ích **Mayzing Session Bridge** trên thanh tiện ích.
3. Đặt **Máy chủ MaZing** là `https://mz.eagleglobal2023.com`, dán mã, bấm **Cho phép tool dùng tab Mayzing này**. Tiện ích nhớ máy chủ đã chọn khi mở lại.
4. Quay lại tool, bấm **Bật tự động upload**. Tool kiểm tra phiên bằng danh sách store trước.
5. Chọn store/origin và sử dụng Prep → Duyệt → Batch & upload như bình thường. Giữ Chrome và tab shop mở trong lúc upload.

Nếu dùng tool local, tạo mã ở `http://127.0.0.1:8780` và đổi máy chủ trong tiện ích thành
`http://127.0.0.1:8780`. Mã phải được tạo trên đúng máy chủ đã chọn. Không cần cài/chạy
tool local trên các máy khách khi dùng bản web. Nút mở shop chỉ mở tab trên trình duyệt hiện tại;
hãy mở dashboard MaZing bằng Google Chrome.

Phiên MaZing được nhận diện bằng cookie của hồ sơ trình duyệt, chưa phải tài khoản đồng bộ giữa
các máy. Xoá cookie, dùng ẩn danh hoặc đổi hồ sơ sẽ mở một phiên khác. Tạo mã và kết nối lại
trên từng máy; không chia sẻ mã / token kết nối.

Mã kết nối có hạn **24 giờ từ lúc tạo**, chỉ dùng **một lần**. Khi đã ghép thành công,
kết nối không bị giới hạn bởi thời hạn mã. Mã và phiên kết nối được giữ trong bộ nhớ;
sau khi khởi động lại tool, tạo mã mới và ghép lại Chrome.

**Tắt tự động** dừng gửi lệnh mới. **Ngắt kết nối** trong tiện ích thu hồi kết nối với tool.
Chrome vẫn mở bình thường. Khi đóng Chrome hoặc khởi động lại tool, ghép lại để dùng tiếp.
Nếu mất kết nối trong lúc gửi thao tác, kiểm tra Task log Mayzing trước khi chạy lại.

## Quyền và dữ liệu

- `activeTab` và `scripting`: chỉ thao tác trên tab Mayzing bạn chủ động chọn khi bấm tiện ích.
- `storage`: lưu token kết nối trong bộ nhớ phiên Chrome; chỉ lưu địa chỉ máy chủ lâu dài, không lưu cookie.
- Host access chỉ dành cho HTTPS `mz.eagleglobal2023.com` và HTTP `127.0.0.1` / `localhost`. Không chấp nhận tên miền khác, thông tin đăng nhập trong URL hoặc đường dẫn tuỳ ý.
- Không yêu cầu quyền `cookies`, `debugger`, history, mật khẩu hay truy cập mọi website.
- Chỉ gửi các thao tác Mayzing được đóng gói sẵn tới `/api/graphql` trên chính tab Mayzing.
- Chọn sản phẩm mặc định dùng `UpdateStoreGroupProductOrder`, giữ đủ các sản phẩm còn lại trong campaign.
- Khi gặp trang xác minh, tiện ích dừng; bạn phải tự hoàn tất bước trên trang.

Nguồn và toàn bộ mã có sẵn trong thư mục này để kiểm tra trước khi cài.
