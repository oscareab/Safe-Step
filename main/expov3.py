import asyncio
import serial
import time
import cv2
from picamera2 import Picamera2
from libcamera import controls
from ultralytics import YOLO
import numpy as np
import ble_server
import atexit
import threading
import queue

# === Initialize hardware ===
print("Initializing cameras...")
camera1 = Picamera2(0)
camera2 = Picamera2(1)
camera1.set_controls({"AfMode": controls.AfModeEnum.Continuous})
camera2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
camera1.start()
camera2.start()
print("Cameras started.")

print("Initializing TF-Luna LiDAR...")
ser = serial.Serial("/dev/ttyAMA0", 115200)

print("Loading YOLO model...")
model_general = YOLO("yolo11n.pt")  # Single YOLO model for general detection

# === Crosswalk Detection Model ===
CROSSWALK_MODEL_PATH = "Crosswalks_ONNX_Model.onnx"
CROSSWALK_INPUT_SIZE = 512
CROSSWALK_CONF_THRESHOLD = 0.3

# === Stereo Calibration ===
calib = np.load("stereo_calib_data.npz")
mtxL, distL = calib['mtxL'], calib['distL']
mtxR, distR = calib['mtxR'], calib['distR']
R, T = calib['R'], calib['T']

# Get image size
frameL = camera1.capture_array()
frameR = camera2.capture_array()
img_size = (frameL.shape[1], frameL.shape[0])

R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(mtxL, distL, mtxR, distR, img_size, R, T)
mapLx, mapLy = cv2.initUndistortRectifyMap(mtxL, distL, R1, P1, img_size, cv2.CV_32FC1)
mapRx, mapRy = cv2.initUndistortRectifyMap(mtxR, distR, R2, P2, img_size, cv2.CV_32FC1)

stereo = cv2.StereoSGBM_create(
    minDisparity=0,
    numDisparities=16 * 4,  # was 16*7
    blockSize=5,
    P1=8 * 3 * 3 ** 2,
    P2=32 * 3 * 3 ** 2,
    disp12MaxDiff=1,
    uniquenessRatio=10,
    speckleWindowSize=50,  # reduced from 100
    speckleRange=2,
    preFilterCap=63,
    mode=cv2.STEREO_SGBM_MODE_SGBM
)

# === LiDAR Reader ===
def read_tfluna_data():
    if ser.in_waiting > 8:
        bytes_serial = ser.read(9)
        ser.reset_input_buffer()
        if bytes_serial[0] == 0x59 and bytes_serial[1] == 0x59:
            distance = bytes_serial[2] + bytes_serial[3] * 256
            strength = bytes_serial[4] + bytes_serial[5] * 256
            temperature = (bytes_serial[6] + bytes_serial[7] * 256) / 8.0 - 256.0
            return {"distance": distance, "strength": strength, "temperature": temperature}
    return None


# === Depth Map Computation ===
def compute_depth_map(imgL, imgR):
    rectL = cv2.remap(imgL, mapLx, mapLy, cv2.INTER_LINEAR)
    rectR = cv2.remap(imgR, mapRx, mapRy, cv2.INTER_LINEAR)
    grayL = cv2.cvtColor(rectL, cv2.COLOR_BGR2GRAY)
    grayR = cv2.cvtColor(rectR, cv2.COLOR_BGR2GRAY)
    disparity = stereo.compute(grayL, grayR).astype(np.float32) / 16.0
    return disparity

def detect_crosswalk(img):
    net = cv2.dnn.readNetFromONNX(CROSSWALK_MODEL_PATH)

    img_resized = cv2.resize(img, (CROSSWALK_INPUT_SIZE, CROSSWALK_INPUT_SIZE))
    blob = cv2.dnn.blobFromImage(img_resized, scalefactor=1/255.0, size=(CROSSWALK_INPUT_SIZE, CROSSWALK_INPUT_SIZE), swapRB=True, crop=False)
    net.setInput(blob)

    output = net.forward()
    output = output.squeeze().transpose(1, 0)

    h_orig, w_orig = img.shape[:2]
    detections = []
    for det in output:
        x, y, w, h, conf = det
        if conf < CROSSWALK_CONF_THRESHOLD:
            continue

        x1 = int((x - w / 2) * w_orig / CROSSWALK_INPUT_SIZE)
        y1 = int((y - h / 2) * h_orig / CROSSWALK_INPUT_SIZE)
        x2 = int((x + w / 2) * w_orig / CROSSWALK_INPUT_SIZE)
        y2 = int((y + h / 2) * h_orig / CROSSWALK_INPUT_SIZE)

        x1, y1 = max(x1, 0), max(y1, 0)
        x2, y2 = min(x2, w_orig - 1), min(y2, h_orig - 1)

        detections.append((x1, y1, x2, y2, conf))

    return detections


# === Main Detection Loop with Optimizations ===
async def capture_and_detect(server: ble_server.SafePiBLEServer):
    i = 0
    valid_disp_min, valid_disp_max = 1, 128
    last_reported_label = None
    last_reported_distance = None  # in cm
    distance_threshold = 100  # Report again only if at least 1 meter closer

    while True:
        print(f"\n--- Frame {i} ---")
        imgL = camera1.capture_array()
        imgR = camera2.capture_array()
        imgL_rgb = cv2.cvtColor(imgL, cv2.COLOR_BGR2RGB)

        annotated_img = imgL.copy()
        disp_color = np.zeros_like(imgL)

        # === Run YOLO and depth map in parallel ===
        def run_yolo():
            small = cv2.resize(imgL_rgb, (320, 320))
            results = model_general(small)[0]

            scale_x = imgL.shape[1] / small.shape[1]
            scale_y = imgL.shape[0] / small.shape[0]

            scaled_boxes = []

            for box in results.boxes:
                if box.conf < 0.7:
                    continue

                coords = box.xyxy[0].clone()
                coords[0] *= scale_x
                coords[1] *= scale_y
                coords[2] *= scale_x
                coords[3] *= scale_y

                x1, y1, x2, y2 = map(int, coords)
                cls_id = int(box.cls[0])
                label = model_general.names[cls_id]

                scaled_boxes.append((x1, y1, x2, y2, label))

                cv2.rectangle(annotated_img, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(annotated_img, f"{label} {box.conf.item():.2f}", (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

            return scaled_boxes


        def run_crosswalk():
            return detect_crosswalk(imgL)

        detection_task = asyncio.to_thread(run_yolo)
        depth_task = asyncio.to_thread(compute_depth_map, imgL, imgR)
        crosswalk_task = asyncio.to_thread(run_crosswalk)
        
        results, disparity, crosswalks = await asyncio.gather(
            asyncio.to_thread(run_yolo),
            asyncio.to_thread(compute_depth_map, imgL, imgR),
            asyncio.to_thread(run_crosswalk)
        )

        results = await results

        disp_vis = cv2.normalize(disparity, None, 0, 255, cv2.NORM_MINMAX)
        disp_vis = np.uint8(disp_vis)
        disp_color = cv2.applyColorMap(disp_vis, cv2.COLORMAP_JET)

        points_3D = cv2.reprojectImageTo3D(disparity, Q)
        detected_objects = []

        for x1, y1, x2, y2, conf in crosswalks:
            # Treat crosswalk like any other label
            region = disparity[y1:y2, x1:x2]
            mask = (region > valid_disp_min) & (region < valid_disp_max)
            if not np.any(mask):
                continue
        
            median_disp = np.median(region[mask])
            if median_disp <= 0:
                continue
        
            temp_disp_map = np.full_like(disparity, median_disp)
            points_median = cv2.reprojectImageTo3D(temp_disp_map, Q)
            center_x = (x1 + x2) // 2
            center_y = (y1 + y2) // 2
            distance_cm = points_median[center_y, center_x][2] * 100
        
            direction = "ahead"
            frame_center_x = imgL.shape[1] // 2
            if center_x < frame_center_x - imgL.shape[1] * 0.2:
                direction = "to the left"
            elif center_x > frame_center_x + imgL.shape[1] * 0.2:
                direction = "to the right"
        
            detected_objects.append({
                "label": "crosswalk",
                "distance_cm": distance_cm,
                "direction": direction
            })
        
            # Optional: Draw rectangle
            cv2.rectangle(annotated_img, (x1, y1), (x2, y2), (255, 255, 0), 2)
            cv2.putText(annotated_img, f"Crosswalk {conf:.2f}", (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 2)


        for x1, y1, x2, y2, label in results:
            region = disparity[y1:y2, x1:x2]
            mask = (region > valid_disp_min) & (region < valid_disp_max)
            if not np.any(mask):
                continue

            median_disp = np.median(region[mask])
            if median_disp <= 0:
                continue

            temp_disp_map = np.full_like(disparity, median_disp)
            points_median = cv2.reprojectImageTo3D(temp_disp_map, Q)
            center_x = (x1 + x2) // 2
            center_y = (y1 + y2) // 2
            distance_cm = points_median[center_y, center_x][2] * 100

            if 0 < distance_cm < 10000:
                direction = "ahead"
                center_x = (x1 + x2) // 2
                frame_center_x = imgL.shape[1] // 2
                
                if center_x < frame_center_x - imgL.shape[1] * 0.2:
                    direction = "to the left"
                elif center_x > frame_center_x + imgL.shape[1] * 0.2:
                    direction = "to the right"
                
                detected_objects.append({
                    "label": label,
                    "distance_cm": distance_cm,
                    "direction": direction
                })


        if not detected_objects:
            print("No YOLO detections, checking closest disparity pixel...")
            mask_valid = (disparity > valid_disp_min) & (disparity < valid_disp_max)
            if np.any(mask_valid):
                distances_cm = points_3D[:, :, 2] * 100
                distances_cm_masked = np.where(mask_valid, distances_cm, np.inf)
                min_idx = np.unravel_index(np.argmin(distances_cm_masked), distances_cm_masked.shape)
                distance_cm = distances_cm[min_idx]
                if 21.0 < distance_cm < 5000:
                    detected_objects.append({"label": "obstacle", "distance_cm": distance_cm})

        if detected_objects:
            detected_objects.sort(key=lambda x: x["distance_cm"])
            closest_object = detected_objects[0]

            lidar_data = read_tfluna_data()
            if lidar_data:
                lidar_distance = lidar_data["distance"]
                if abs(lidar_distance - closest_object["distance_cm"]) > 100:
                    print(f"LiDAR discrepancy ({lidar_distance} cm), overriding.")
                    closest_object["distance_cm"] = lidar_distance

            should_report = False
            if (last_reported_label != closest_object["label"]):
                should_report = True
            elif (last_reported_distance is not None and
                  last_reported_distance - closest_object["distance_cm"] >= distance_threshold):
                should_report = True

            if should_report:
                direction = closest_object.get("direction", "ahead")
                print(f"→ Closest: {closest_object['label']} @ {closest_object['distance_cm']:.1f} cm to the {direction}")
                current_time = time.time()
                if not hasattr(capture_and_detect, "last_sent_time"):
                    capture_and_detect.last_sent_time = 0

                if current_time - capture_and_detect.last_sent_time >= 5:
                    await server.send_message(
                        f"{closest_object['label']} {direction}, {closest_object['distance_cm'] / 100:.1f} meters away"
                    )
                    capture_and_detect.last_sent_time = current_time
                
                await asyncio.sleep(0)

                last_reported_label = closest_object["label"]
                last_reported_distance = closest_object["distance_cm"]
            else:
                print(f"→ {closest_object['label']} @ {closest_object['distance_cm']:.1f} cm (not reported)")


        # === Show updated frames with OpenCV ===
        # cv2.imshow("YOLO Detection", annotated_img)
        #cv2.imshow("Depth Map", disp_color)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

        i += 1
        await asyncio.sleep(0)


# === Main Entrypoint ===
async def main():
    try:
        loop = asyncio.get_running_loop()
        server = ble_server.SafePiBLEServer(loop)
        
        await server.start()
        await capture_and_detect(server)
    except KeyboardInterrupt:
        print("\nInterrupted. Shutting down...")
    finally:
        camera1.stop()
        camera2.stop()
        ser.close()
        cv2.destroyAllWindows()
        print("Cameras and LiDAR stopped.")


if __name__ == "__main__":
    asyncio.run(main())
