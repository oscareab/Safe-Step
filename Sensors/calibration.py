import cv2
import numpy as np
import glob

# Settings
chessboard_size = (9, 6)
square_size = 0.025  # in meters (2.5 cm)
image_format = 'jpg'

# Prepare object points, like (0,0,0), (1,0,0), ..., (8,5,0)
objp = np.zeros((chessboard_size[0]*chessboard_size[1], 3), np.float32)
objp[:, :2] = np.mgrid[0:chessboard_size[0], 0:chessboard_size[1]].T.reshape(-1, 2)
objp *= square_size

objpoints = []  # 3D points in real world
imgpoints_left = []  # 2D points from left cam
imgpoints_right = []  # 2D points from right cam

images_left = sorted(glob.glob('left/*.' + image_format))
images_right = sorted(glob.glob('right/*.' + image_format))

for imgL_path, imgR_path in zip(images_left, images_right):
    imgL = cv2.imread(imgL_path)
    imgR = cv2.imread(imgR_path)
    grayL = cv2.cvtColor(imgL, cv2.COLOR_BGR2GRAY)
    grayR = cv2.cvtColor(imgR, cv2.COLOR_BGR2GRAY)

    retL, cornersL = cv2.findChessboardCorners(grayL, chessboard_size, None)
    retR, cornersR = cv2.findChessboardCorners(grayR, chessboard_size, None)

    if retL and retR:
        objpoints.append(objp)
        imgpoints_left.append(cornersL)
        imgpoints_right.append(cornersR)

        cv2.drawChessboardCorners(imgL, chessboard_size, cornersL, retL)
        cv2.drawChessboardCorners(imgR, chessboard_size, cornersR, retR)
        cv2.imshow('Left', imgL)
        cv2.imshow('Right', imgR)
        cv2.waitKey(100)

cv2.destroyAllWindows()

# Calibrate single cameras
retL, mtxL, distL, _, _ = cv2.calibrateCamera(objpoints, imgpoints_left, grayL.shape[::-1], None, None)
retR, mtxR, distR, _, _ = cv2.calibrateCamera(objpoints, imgpoints_right, grayR.shape[::-1], None, None)

# Stereo calibration
flags = cv2.CALIB_FIX_INTRINSIC
criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-5)
retS, _, _, _, _, R, T, E, F = cv2.stereoCalibrate(
    objpoints, imgpoints_left, imgpoints_right,
    mtxL, distL, mtxR, distR,
    grayL.shape[::-1],
    criteria=criteria, flags=flags
)

# Stereo rectification
R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(mtxL, distL, mtxR, distR, grayL.shape[::-1], R, T)

# Save calibration
np.savez('stereo_calib_data.npz', mtxL=mtxL, distL=distL, mtxR=mtxR, distR=distR, R=R, T=T, R1=R1, R2=R2, P1=P1, P2=P2, Q=Q)
print("Calibration done and saved.")
