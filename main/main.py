import asyncio
import serial
import time
import cv2
from picamera2 import Picamera2
from libcamera import controls
from ultralytics import YOLO
import numpy as np
import matplotlib.pyplot as plt
import ble_server
import time

# Initialize hardware
print("Initializing cameras...")
camera1 = Picamera2(0)
camera2 = Picamera2(1)

camera1.set_controls({"AfMode": controls.AfModeEnum.Continuous})
camera2.set_controls({"AfMode": controls.AfModeEnum.Continuous})

camera1.start()
camera2.start()
print("Cameras initialized and started.")

print("Initializing TF-Luna LiDAR...")
ser = serial.Serial("/dev/ttyAMA0", 115200)

print("Loading YOLO models...")
model_general = YOLO("yolo11s.pt")          # COCO (people, cars, etc.)
model_crosswalk = YOLO("yolov8n.pt")         # Your crosswalk model

# Load stereo calibration data
calib = np.load("stereo_calib_data.npz")
mtxL, distL = calib['mtxL'], calib['distL']
mtxR, distR = calib['mtxR'], calib['distR']
R, T = calib['R'], calib['T']

# Get image size
frameL = camera1.capture_array()
frameR = camera2.capture_array()
img_size = (frameL.shape[1], frameL.shape[0])

# Stereo rectify
R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(mtxL, distL, mtxR, distR, img_size, R, T)

# Precompute rectification maps
mapLx, mapLy = cv2.initUndistortRectifyMap(mtxL, distL, R1, P1, img_size, cv2.CV_32FC1)
mapRx, mapRy = cv2.initUndistortRectifyMap(mtxR, distR, R2, P2, img_size, cv2.CV_32FC1)

# StereoSGBM matcher
stereo = cv2.StereoSGBM_create(
    minDisparity=0,
    numDisparities=16 * 8,
    blockSize=3,
    P1=8 * 3 * 3 ** 2,
    P2=32 * 3 * 3 ** 2,
    disp12MaxDiff=1,
    uniquenessRatio=10,
    speckleWindowSize=100,
    speckleRange=2,
    preFilterCap=63,
    mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY
)

HAZARD_DISTANCE_CM = 500 

def read_tfluna_data():
    output = {}
    counter = ser.in_waiting
    if counter > 8:
        bytes_serial = ser.read(9)
        ser.reset_input_buffer()

        if bytes_serial[0] == 0x59 and bytes_serial[1] == 0x59:
            distance = bytes_serial[2] + bytes_serial[3] * 256
            strength = bytes_serial[4] + bytes_serial[5] * 256
            temperature = bytes_serial[6] + bytes_serial[7] * 256
            temperature = (temperature / 8.0) - 256.0
            output = {
                "distance": distance,
                "strength": strength,
                "temperature": temperature
            }
    return output

def compute_depth_map(imgL, imgR):
    rectL = cv2.remap(imgL, mapLx, mapLy, cv2.INTER_LINEAR)
    rectR = cv2.remap(imgR, mapRx, mapRy, cv2.INTER_LINEAR)
    
    grayL = cv2.cvtColor(rectL, cv2.COLOR_BGR2GRAY)
    grayR = cv2.cvtColor(rectR, cv2.COLOR_BGR2GRAY)

    disparity = stereo.compute(grayL, grayR).astype(np.float32) / 16.0
    print(f"{np.min(disparity)} {np.max(disparity)} {np.mean(disparity)}" )
    return disparity

def get_object_distance(bbox, disparity_map, Q):
    x1, y1, x2, y2 = map(int, bbox)
    region = disparity_map[y1:y2, x1:x2]

    valid_disp_min = 1
    valid_disp_max = 128

    mask = (region > valid_disp_min) & (region < valid_disp_max)

    if np.count_nonzero(mask) == 0:
        return None

    disp_valid = region[mask]
    median_disp = np.median(disp_valid)

    points_3D = cv2.reprojectImageTo3D(disparity_map, Q)

    center_x = (x1 + x2) // 2
    center_y = (y1 + y2) // 2

    center_disp = disparity_map[center_y, center_x]

    distance_from_center = None
    if valid_disp_min < center_disp < valid_disp_max:
        point_center = points_3D[center_y, center_x]
        distance_from_center = point_center[2] * 100

    if median_disp > 0:
        temp_disp_map = np.full_like(disparity_map, median_disp)
        points_median = cv2.reprojectImageTo3D(temp_disp_map, Q)
        distance_from_median = points_median[center_y, center_x][2] * 100
    else:
        distance_from_median = None

    distances = []
    if distance_from_center is not None and 0 < distance_from_center < 5000:
        distances.append(distance_from_center)
    if distance_from_median is not None and 0 < distance_from_median < 5000:
        distances.append(distance_from_median)

    if distances:
        return np.median(distances)
    else:
        return None
    
def get_direction(x_center, image_width):
    if x_center < image_width / 3:
        return "left"
    elif x_center > 2 * image_width / 3:
        return "right"
    else:
        return "ahead"

def is_hazard(obj):
    return obj["distance_cm"] < HAZARD_DISTANCE_CM

async def capture_and_detect(server: ble_server.SafePiBLEServer):
    i = 0
    while True:
        print(f"\n--- Capture {i} ---")
        print("Capturing images...")
        imgL = camera1.capture_array()
        imgR = camera2.capture_array()
        imgL_rgb = cv2.cvtColor(imgL, cv2.COLOR_BGR2RGB)

        print("Running object detection (YOLO11s)...")
        results_general = model_general(imgL_rgb)[0]
        asyncio.sleep(0)

        print("Running crosswalk detection (YOLOv8n)...")
        results_crosswalk = model_crosswalk(imgL_rgb)[0]
        asyncio.sleep(0)

        print("Computing depth map...")
        disparity = compute_depth_map(imgL, imgR)
        asyncio.sleep(0)

        detected_objects = []

        valid_disp_min = 1
        valid_disp_max = 128

        points_3D = cv2.reprojectImageTo3D(disparity, Q)

        # Step 1: Analyze all YOLO detections
        for results, model in [(results_general, model_general), (results_crosswalk, model_crosswalk)]:
            for box in results.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cls_id = int(box.cls[0])
                label = model.names[cls_id]

                region = disparity[y1:y2, x1:x2]
                mask = (region > valid_disp_min) & (region < valid_disp_max)

                if np.count_nonzero(mask) == 0:
                    continue

                disp_valid = region[mask]
                median_disp = np.median(disp_valid)

                temp_disp_map = np.full_like(disparity, median_disp)
                points_median = cv2.reprojectImageTo3D(temp_disp_map, Q)
                center_x = (x1 + x2) // 2
                center_y = (y1 + y2) // 2
                distance_cm = points_median[center_y, center_x][2] * 100  # meters to cm

                if distance_cm <= 0 or distance_cm > 5000:
                    continue

                x1, y1, x2, y2 = map(int, box.xyxy[0])
                center_x = (x1 + x2) // 2
                direction = get_direction(center_x, imgL.shape[1])

                detected_objects.append({
                    "label": label,
                    "distance_cm": distance_cm,
                    "direction": direction
                })


        # Step 2: If no YOLO detections found, fallback to closest pixel
        if not detected_objects:
            print("No YOLO detections, falling back to closest depth pixel.")
        
            mask_valid = (disparity > valid_disp_min) & (disparity < valid_disp_max)
        
            if not np.any(mask_valid):
                print("No valid disparity points found.")
                i += 1
                continue
        
            # Find the valid pixel with the minimum Z-distance (closest)
            distances_cm = points_3D[:, :, 2] * 100
            distances_cm_masked = np.where(mask_valid, distances_cm, np.inf)
            min_idx = np.unravel_index(np.argmin(distances_cm_masked), distances_cm_masked.shape)
            center_y, center_x = min_idx
        
            distance_cm = distances_cm[center_y, center_x]
        
            # Reject the border artifacts (~20.7 cm constant)
            if distance_cm <= 21.0:
                print(f"Closest point ({distance_cm:.1f} cm) is likely border noise. Skipping...")
                i += 1
                continue
        
            if distance_cm <= 0 or distance_cm > 5000:
                print("Depth map distance invalid or too far.")
                i += 1
                continue
        
            detected_objects.append({
                "label": "obstacle",
                "distance_cm": distance_cm
            })


        # Step 3: Choose the object with the minimum distance
        detected_objects = sorted(detected_objects, key=lambda x: x["distance_cm"])
        closest_object = detected_objects[0]

        # Step 4: Cross-check with LiDAR
        lidar_data = read_tfluna_data()
        if lidar_data:
            lidar_distance_cm = lidar_data["distance"]
            if abs(lidar_distance_cm - closest_object["distance_cm"]) > 100:
                print(f"Depth map distance ({closest_object['distance_cm']:.1f} cm) differs from LiDAR ({lidar_distance_cm:.1f} cm). Using LiDAR.")
                closest_object["distance_cm"] = lidar_distance_cm
            else:
                print("Depth map and LiDAR distances match within tolerance.")
                
        hazards = [obj for obj in detected_objects if is_hazard(obj)]

        if not hazards:
            print("No hazards detected.")
            await asyncio.sleep(0)
            continue

        # Step 5: Report
        print("\nClosest Object Detected:")
        print(f"Label: {closest_object['label']}")
        print(f"Distance: {round(closest_object['distance_cm'], 1)} cm")
        print(f"Direction: {closest_object['direction']}")
        await server.send_message(f"{closest_object['label']} found {round(closest_object['distance_cm'] / 100, 1)} meters {closest_object['direction']}")
        await asyncio.sleep(0)

        i += 1

async def main():
    try:
        loop = asyncio.get_running_loop()
        server = ble_server.SafePiBLEServer(loop)
        
        await server.start()
        await capture_and_detect(server)
    except KeyboardInterrupt:
        print("\nStopping program...")
    finally:
        camera1.stop()
        camera2.stop()
        ser.close()
        print("Cameras and LiDAR stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down...")
