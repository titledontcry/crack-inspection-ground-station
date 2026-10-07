#!/usr/bin/env python3
"""Jetson Nano companion agent for Crack Inspection Ground Station.

One process owns the downward USB camera. It serves an MJPEG live view and
periodically writes a full-resolution JPEG into a durable spool. A second
thread uploads the spool to the ground station and retries while Wi-Fi is
unavailable. It deliberately contains no Pixhawk commands and no AI model.

Compatible with the Python 3.6 shipped by JetPack 4.6.
"""
from __future__ import print_function

import argparse
import datetime
import glob
import http.server
import json
import os
import shutil
import socketserver
import sys
import threading
import time

import cv2
import requests


class CameraAgent(object):
    def __init__(self, args):
        self.args = args
        self.latest_jpeg = None
        self.lock = threading.Lock()
        self.error = None
        self.captured = 0
        self.uploaded = 0
        self.spool = os.path.abspath(args.spool)
        self.sent = os.path.join(self.spool, "sent")
        self.pending = os.path.join(self.spool, "pending")
        for directory in (self.pending, self.sent):
            if not os.path.isdir(directory):
                os.makedirs(directory)

    def start(self):
        thread = threading.Thread(target=self._capture_loop, name="camera")
        thread.daemon = True
        thread.start()
        worker = threading.Thread(target=self._upload_loop, name="uploader")
        worker.daemon = True
        worker.start()

    def _capture_loop(self):
        cap = cv2.VideoCapture(self.args.camera, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.args.height)
        cap.set(cv2.CAP_PROP_FPS, self.args.fps)
        if not cap.isOpened():
            self.error = "cannot open camera %s" % self.args.camera
            return
        next_capture = time.monotonic()
        while True:
            ok, frame = cap.read()
            if not ok:
                self.error = "camera frame read failed"
                time.sleep(0.2)
                continue
            self.error = None
            preview = cv2.resize(frame, (self.args.preview_width, self.args.preview_height))
            ok, buf = cv2.imencode(".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), self.args.preview_quality])
            if ok:
                with self.lock:
                    self.latest_jpeg = buf.tobytes()
            now = time.monotonic()
            if now >= next_capture:
                self._spool_frame(frame)
                next_capture = now + self.args.capture_interval

    def _spool_frame(self, frame):
        captured_at = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        name = "crack_%s_%06d.jpg" % (datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S"), self.captured)
        final_image = os.path.join(self.pending, name)
        # OpenCV selects an encoder from the suffix, so the temporary file must
        # still end in .jpg.
        temp_image = final_image + ".part.jpg"
        if not cv2.imwrite(temp_image, frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.args.photo_quality]):
            self.error = "could not write %s" % final_image
            return
        os.rename(temp_image, final_image)  # uploader only sees complete images
        metadata = {
            "captured_at": captured_at,
            "camera": self.args.camera,
            "image_width_px": int(frame.shape[1]),
            "image_height_px": int(frame.shape[0]),
            "capture_interval_s": self.args.capture_interval,
        }
        with open(final_image + ".json", "w") as f:
            json.dump(metadata, f)
        self.captured += 1
        print("captured", name)

    def _upload_loop(self):
        while True:
            images = sorted(glob.glob(os.path.join(self.pending, "*.jpg")))
            if not images:
                time.sleep(0.5)
                continue
            path = images[0]
            try:
                with open(path + ".json") as f:
                    metadata = json.load(f)
                with open(path, "rb") as image_file:
                    response = requests.post(
                        self.args.endpoint.rstrip("/") + "/api/images",
                        files={"file": (os.path.basename(path), image_file, "image/jpeg")},
                        data={"metadata": json.dumps(metadata)},
                        timeout=(5, self.args.upload_timeout),
                    )
                response.raise_for_status()
                shutil.move(path, os.path.join(self.sent, os.path.basename(path)))
                shutil.move(path + ".json", os.path.join(self.sent, os.path.basename(path) + ".json"))
                self.uploaded += 1
                self._trim_sent()
                print("uploaded", os.path.basename(path))
            except Exception as exc:
                self.error = "upload failed: %s" % exc
                print(self.error, file=sys.stderr)
                time.sleep(self.args.retry_seconds)

    def _trim_sent(self):
        if self.args.keep_sent <= 0:
            return
        images = sorted(glob.glob(os.path.join(self.sent, "*.jpg")))
        for path in images[:-self.args.keep_sent]:
            try:
                os.remove(path)
                os.remove(path + ".json")
            except OSError:
                pass

    def status(self):
        with self.lock:
            live = self.latest_jpeg is not None
        return {"live": live, "error": self.error, "captured": self.captured,
                "uploaded": self.uploaded, "pending": len(glob.glob(os.path.join(self.pending, "*.jpg")))}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    agent = None

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(self.agent.status()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/live":
            self._mjpeg()
        else:
            self.send_response(302)
            self.send_header("Location", "/live")
            self.end_headers()

    def _mjpeg(self):
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                with self.agent.lock:
                    jpeg = self.agent.latest_jpeg
                if jpeg is None:
                    time.sleep(.1)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpeg)).encode("ascii"))
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                time.sleep(1.0 / max(self.agent.args.preview_fps, 1))
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Jetson camera uploader for Crack Inspection Ground Station")
    parser.add_argument("--endpoint", required=True, help="Ground station URL, e.g. http://192.168.1.10:8000")
    parser.add_argument("--camera", default="/dev/cam_down", help="Downward camera device")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=15, help="Camera acquisition FPS")
    parser.add_argument("--capture-interval", type=float, default=5.0, help="Seconds between high-res inspection photos")
    parser.add_argument("--photo-quality", type=int, default=92)
    parser.add_argument("--preview-width", type=int, default=640)
    parser.add_argument("--preview-height", type=int, default=360)
    parser.add_argument("--preview-fps", type=int, default=8)
    parser.add_argument("--preview-quality", type=int, default=70)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--spool", default="/home/arnon/crack-ground-station/spool")
    parser.add_argument("--keep-sent", type=int, default=0, help="0 keeps every source image; otherwise retain this many")
    parser.add_argument("--retry-seconds", type=float, default=3.0)
    parser.add_argument("--upload-timeout", type=float, default=60.0)
    args = parser.parse_args()
    if not os.path.exists(args.camera):
        parser.error("camera is not present: %s" % args.camera)
    if args.capture_interval <= 0:
        parser.error("capture interval must be positive")
    agent = CameraAgent(args)
    agent.start()
    Handler.agent = agent
    print("Jetson crack agent: http://0.0.0.0:%d/live" % args.port)
    print("camera=%s, photo every %.1f s, ground=%s" % (args.camera, args.capture_interval, args.endpoint))
    Server(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
