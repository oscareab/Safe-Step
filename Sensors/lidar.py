import serial
import time
ser = serial.Serial("/dev/ttyAMA0", 115200)

# we define a new function that will get the data from LiDAR and publish it
def read_data():
    time.sleep(1)  # Sleep 1000ms
    while True:
        counter = ser.in_waiting # count the number of bytes of the serial port
        if counter > 8:
            bytes_serial = ser.read(9)
            ser.reset_input_buffer()
            
            if bytes_serial[0] == 0x59 and bytes_serial[1] == 0x59: # python3
                distance = bytes_serial[2] + bytes_serial[3]*256
                strength = bytes_serial[4] + bytes_serial[5]*256
                temperature = bytes_serial[6] + bytes_serial[7]*256 # For TFLuna
                temperature = (temperature/8) - 256
                print("TF-Luna python3 portion")
                print("Distance:"+ str(distance) + "cm")
                print("Strength:" + str(strength))
                if temperature != 0:
                    print("Chip Temperature:" + str(temperature)+ "℃")
                ser.reset_input_buffer()
                
            if bytes_serial[0] == "Y" and bytes_serial[1] == "Y":
                distL = int(bytes_serial[2].encode("hex"), 16)
                distH = int(bytes_serial[3].encode("hex"), 16)
                stL = int(bytes_serial[4].encode("hex"), 16)
                stH = int(bytes_serial[5].encode("hex"), 16)
                distance = distL + distH*256
                strength = stL + stH*256
                tempL = int(bytes_serial[6].encode("hex"), 16)
                tempH = int(bytes_serial[7].encode("hex"), 16)
                temperature = tempL + tempH*256
                temperature = (temperature/8) - 256
                print("TF-Luna python2 portion")
                print("Distance:"+ str(distance) + "cm\n")
                print("Strength:" + str(strength) + "\n")
                print("Chip Temperature:" + str(temperature) + "℃\n")
                ser.reset_input_buffer()
if __name__ == "__main__":
    try:
        if ser.isOpen() == False:
            print("opening serial")
            ser.open()
        print("reading data")
        read_data()
    except KeyboardInterrupt:
        if ser != None:
            ser.close()
            print("program interrupted by the user")

