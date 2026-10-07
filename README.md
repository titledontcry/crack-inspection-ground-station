# Drone inspection dashboard

ระบบนี้รันบน **คอมภาคพื้นดิน**: Jetson Nano ส่งภาพนิ่งจากโดรนเข้ามาทาง Wi-Fi, คอมเข้าแถวประมวลผลด้วยโมเดล segmentation, แล้ว Dashboard แสดงภาพ live และผลตรวจตามลำดับภาพ

Pixhawk 2 ทำหน้าที่ควบคุมการบิน; Jetson Nano ควรเป็น companion computer ที่ต่อกล้องและเครือข่าย ไม่ต้องรันโมเดล segmentation บน Jetson ในเวอร์ชันนี้

## สิ่งที่ระบบรับประกัน

- ภาพที่ Jetson ส่งมาได้รับ `image_id` ทันทีและเข้าคิวแบบ FIFO
- เก็บไฟล์ original ไว้ที่คอมก่อนเริ่มทำนาย
- หน้าเว็บมี live-stream แยกจากรายการภาพตรวจ จึงไม่ทำให้ inference ที่ช้ากระทบวิดีโอควบคุมการบิน
- ทุกแถวมีสถานะ `queued`, `processing`, `done`, หรือ `failed` และคลิกเพื่อดู original/overlay/mask ได้
- ผลวัดเก็บเป็น pixel เสมอ; ระบบจะแปลงเป็น mm ก็ต่อเมื่อ Jetson ส่ง `gsd_mm_per_px` ที่น่าเชื่อถือมา

## เริ่มใช้งานบนคอมภาคพื้นดิน

จาก root ของ repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r drone_inference/requirements.txt
cp drone_inference/config.example.yaml drone_inference/config.yaml
uvicorn drone_inference.app:app --host 0.0.0.0 --port 8000
```

เปิด `http://<computer-ip>:8000` จากคอมหรืออุปกรณ์ใน Wi-Fi เดียวกัน

ก่อนรันจริง แก้ `checkpoint` ใน `config.yaml` ให้ชี้ไปยัง `best.pt` ที่ผ่าน Final Test แล้ว และตั้ง `live_stream_url` เป็น MJPEG/HLS/WebRTC gateway ของ Jetson ตามที่ใช้จริง

## Demo โมเดลด้วย test set 5 ภาพ

เมื่อเปิด dashboard แล้ว ให้กดปุ่ม **DEMO 5 TEST IMAGES** ในส่วน Inspection results. ระบบจะสุ่มภาพ 5 ภาพแบบกำหนด seed จาก `manifest_v2.csv` ที่มี split `test`, copy เข้า runtime storage แล้วประมวลผลเหมือนภาพที่ Jetson ส่งจริงทีละภาพ

ภาพ prediction จะมีทั้ง mask สีแดงและกรอบสีเขียวรอบ connected component ของรอยร้าว. กรอบมีไว้ช่วยกวาดตาเท่านั้น; mask คือผล segmentation ที่ใช้วัดจริง. หากปุ่มแจ้งว่าไม่พบ dataset ให้ตรวจ `demo_dataset_dir` ใน `config.yaml`.

## สัญญา API สำหรับ Jetson Nano

ส่งภาพนิ่งหลังกล้องบันทึกภาพ (ไม่ใช่ทุก frame ของ live video):

```bash
curl -X POST http://<ground-computer-ip>:8000/api/images \
  -F 'file=@DJI_0001.jpg' \
  -F 'metadata={"captured_at":"2026-10-04T10:30:00Z","latitude":13.7563,"longitude":100.5018,"distance_to_surface_m":5.2,"gsd_mm_per_px":0.42}'
```

`metadata` เป็น JSON และไม่บังคับทุก field. ให้คำนวณ `gsd_mm_per_px` บน Jetson หรือส่ง camera metadata ที่เพียงพอมาในรุ่นถัดไป. ห้ามใช้ GPS altitude แทนระยะถึงผิวเมื่อบินตรวจผนัง/สะพาน

ตัวตอบกลับมี `image_id` และ `status`; Jetson สามารถเรียก `GET /api/images/{image_id}` เพื่อตรวจผลภายหลังได้

มีตัวส่งไฟล์แบบ watch-folder สำหรับ Jetson ที่ [jetson_sender.py](jetson_sender.py): ให้โปรแกรมกล้องบันทึกภาพนิ่งลงโฟลเดอร์ แล้วรัน

```bash
python3 drone_inference/jetson_sender.py \
  --endpoint http://<ground-computer-ip>:8000 \
  --source /path/to/camera/stills
```

ตัวส่งจะ retry เมื่อ Wi-Fi หลุดและไม่ลบ original บน Jetson. การเชื่อม telemetry จาก Pixhawk (GPS, ระยะถึงผิว และ GSD) ยังต้องเติมให้ตรงกับกล้องและวิธีวัดระยะที่ติดตั้งจริง

## ภาพ live จาก Jetson

ให้ Jetson เผย video stream ด้วยวิธีที่กล้องรองรับ (เช่น MJPEG server หรือ RTSP-to-HLS gateway) แล้วใส่ URL ใน `live_stream_url`. Dashboard แสดงเฉพาะ stream นั้น ไม่ส่งภาพวิดีโอผ่าน API upload นี้ เพื่อไม่แย่ง bandwidth กับภาพนิ่งสำหรับวัดผล

สำหรับ Jetson Nano / JetPack 4.6 ใช้ [jetson/](jetson/) ได้โดยตรง: ตัว agent เปิดกล้องเพียง process เดียว, ปล่อย MJPEG และส่งภาพนิ่งเป็นระยะไปยัง API นี้ โดยไม่ยุ่งกับ Pixhawk หรือรันโมเดลบน Jetson.

## โฟลเดอร์ข้อมูล runtime

`data/` และ `config.yaml` ถูก ignore จาก Git:

- `data/originals/` ภาพต้นฉบับที่รับมา
- `data/results/` overlay, mask และ JSON
- `data/inspection.sqlite3` สถานะคิวและผลวัด

## ข้อจำกัดปัจจุบัน

โมเดลใช้ checkpoint U-Net เดิมและ sliding window 448 px with 25% overlap. Threshold ค่าเริ่มต้น 0.1 มาจาก validation เท่านั้น; ต้อง freeze ค่านี้หลัง Final Test ก่อนนำไปตรวจงานจริง. ค่า length/width จาก mask ยังเป็นการวัดเชิงภาพ ต้อง validate ด้วย drone field test และ crack gauge.
# crack-inspection-ground-station
