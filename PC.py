import tkinter as tk
import shlex
import subprocess
import time
import math
import random
import screenGeo
import sys
import msvcrt
import json
import time
import datetime
import sys
import win32pipe, win32file, pywintypes
import pynput
import glob
from pynput.keyboard import Key, Listener
import os 
import threading
global totalGirls
totalGirls = 0

def sleep_better(duration, get_now=time.perf_counter):
    now = get_now()
    end = now + duration
    while now < end:
        now = get_now()
def on_press(key):
    print
    arr = []
    print (totalGirls)
    for j in range(0, totalGirls):
        arr.append(win32file.CreateFile(
                fr'\\.\pipe\mpvsocket{j}',
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_DELETE | win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                0,
                None
            ))
    if 'char' in dir(key):
        if key.char == 'q':
            [win32file.WriteFile(socket, b'quit \n') for socket in arr]
            return False 

def on_release(key):
    print('{0} release'.format(
        key))
    if key == Key.esc:
        # Stop listener
        return False

def main():
    # sleep_better(10)
    if cycledGirls > 1:
        thread = threading.Thread(target=script)
        thread.start()
        with Listener(
            on_press=on_press,
            on_release=on_release) as listener:
            listener.join()
B = "B"
P = "P"
PC = "PC"
PD = "PD"
M = "M"
RC = "RC"
PM = "PM"
D = "D"
CP = "CP"
CSP = "CSP"
CS = "CS"
brefoless = "DB"
masterType = input("What SP do you want? LMFAO: ").upper()
if not masterType:
    masterType = RC
columnGrid = int(input("Horizontal Videos: ") or 1)
rowGrid = int(input("Vertical Videos: ") or 1)
inputGeometry = screenGeo.get_curr_screen_geometry()
inputGeometry[0] = int(
    input("What is your X screen size: ") or inputGeometry[0])
inputGeometry[1] = int(
    input("What is your Y screen size: ") or inputGeometry[1])
cycledGirls = int(input("Number of girls to cycle through ") or 1)
intervalCycle = 15
totalTime = 300
if cycledGirls > 1:
    if masterType != 'CSP':
        intervalCycle = int(input("How long do you wanna watch each girl before switching to another?") or 15)
        totalTime = int(input("How long do you think your session will be?") or 300)
# reserved for CSP
if masterType == 'CSP':
    intervalCycle = 28
    totalTime = intervalCycle * cycledGirls
pixelDropMap = {PC: 50, RC: 100, PM: 0, D: 100, M: 130, CSP: 200}
pixelDrop = pixelDropMap[masterType]
cropHeight = .88
calculatedValue = ((inputGeometry[0] / columnGrid) / ((inputGeometry[1] / rowGrid) + pixelDrop) * (inputGeometry[1] * cropHeight)) / inputGeometry[0]

cropMap = {PC:calculatedValue, M: calculatedValue, RC: calculatedValue, PM: 1, D: calculatedValue, CSP: calculatedValue}
defaultDurationMap = {PC: 60, M: 360, RC: 360, PM: 60, D: 360, PD: 60, CP: intervalCycle, CSP: intervalCycle , CS: intervalCycle, B: 180, "BP": 60, "BEP": 180}
commandStatement = "mpv --start="
cropWidth = str(cropMap[masterType] if masterType in cropMap else .8)
cropHeight = str(cropHeight)
production = True
testMode = False


lavfi = f"crop={cropWidth}*in_w:{cropHeight}*in_h:y=0"
# --no-keepaspect if you want it to not stretch to screen
# we use panscan to keep the people looking as good as possible
# keepaspsect and panscan=1 if you want it to fit as best as possible.
pcParameters = f"--no-border --video-sync=display-resample --interpolation --keepaspect --no-keepaspect-window --panscan=1 "
if testMode:
    pcParameters += "--no-video "
if testMode:
    pcParameters += "--no-audio "
if masterType == PC:
    pcParameters += "--brightness=6 --gamma=7 "
# q
# Converted to json
with open('systemFileMap.json') as d:
    systemFileMap = json.load(d)
allFiles = glob.glob('*.mp4')
getDiff = list(set(allFiles) - set(systemFileMap.keys()))
print(getDiff)
getDiffOpposite = list(set(systemFileMap.keys()) - set(allFiles))
print(getDiffOpposite)
# print(systemFileMap)

for stuff in getDiffOpposite:
    systemFileMap.pop(stuff, None)

argsFileMap = {}
n = len(sys.argv)
for k in range(1, n):
    for key, val in systemFileMap.copy().items():
        if sys.argv[k] in key:
            argsFileMap[key] = val
            systemFileMap.pop(key)
            break
    print(key)
# print(argsFileMap)
a = list(argsFileMap.items())
random.shuffle(a)
argsFileMap = dict(a)
print(len(systemFileMap))
l = list(systemFileMap.items())
random.shuffle(l)
systemFileMap = dict(l)
systemFileMap.update(argsFileMap)
# print(systemFileMap)
# print(systemFileMap)
# Build up the geometry
Geometry = " --geometry="
inputWidth = inputGeometry[0]
inputHeight = inputGeometry[1]
outputWidth = math.ceil(inputWidth / columnGrid) + inputGeometry[2]
outputHeight = math.ceil(inputHeight / rowGrid) + inputGeometry[3]
Geometry += str(outputWidth) + "x" + str(outputHeight) + "+"

sfmList = systemFileMap.items()
girlSocketMap = {}

def convertTimeToMS(timeFormat):
    return (datetime.datetime.strptime(timeFormat,"%M:%S"))

def getDurationOfSession(time1, time2):
    duration = convertTimeToMS(time1) - convertTimeToMS(time2)
    return int(duration.total_seconds())

for i in range(0, rowGrid):
    for j in range(0, columnGrid):
        widthPosition = outputWidth * j
        heightPosition = outputHeight * i
        tempGeometry = Geometry
        tempGeometry += str(widthPosition) + "+" + str(heightPosition) + " "
        filename = ""
        startTime = {}
        while (masterType not in startTime):
            if len(systemFileMap) == 0:
                break
            filename, newStartTime = systemFileMap.popitem()
            print (filename, newStartTime)
            if masterType in newStartTime:
                if newStartTime[masterType] == "0":
                    continue
                if masterType == 'CSP':
                    cspStartTime = datetime.datetime.strptime(newStartTime[masterType],"%M:%S") - datetime.timedelta(seconds=3)
                    newStartTime[masterType] = cspStartTime.strftime("%M:%S")
            startTime = newStartTime
            if testMode:
                startTime[masterType] = "0"
        # print(startTime)
        if masterType in startTime:
            print("SOCKET " + str(totalGirls) + " created")
            socketParams = f"--input-ipc-server=mpvsocket{totalGirls} "
            if not testMode:
                if "E" + masterType not in newStartTime:
                    newStartTime["videoTime"] = defaultDurationMap[masterType]
                else:
                    newStartTime["videoTime"] = getDurationOfSession(newStartTime["E" + masterType], newStartTime[masterType])
                    if masterType in [M, D]:
                        if P+masterType in newStartTime:
                            newStartTime[B + masterType] = getDurationOfSession(newStartTime[P+masterType], newStartTime[masterType])
                            newStartTime[B + P + masterType] = getDurationOfSession(newStartTime["E"+P+masterType], newStartTime[P+masterType])
                            newStartTime[B + "E" + P + masterType] = getDurationOfSession(newStartTime["E"+masterType], newStartTime["E"+P+masterType])
                girlSocketMap[totalGirls] = [{filename: newStartTime}]
            tempLavfi = lavfi
            if "flip" in startTime:
                tempLavfi += ",hflip"
            if "addOn" in startTime:
                pcParameters += startTime["addOn"]
            ffmpegParams = f"--vf=lavfi=[{tempLavfi}]"
            finalStatement = shlex.split(commandStatement + str(
                startTime[masterType]) + " " + filename + tempGeometry + pcParameters + socketParams + ffmpegParams)
            finalStatementString = commandStatement + \
                str(startTime[masterType]) + " " + filename + \
                tempGeometry + pcParameters + socketParams + ffmpegParams
            print(finalStatementString)
            if production:
                p = subprocess.Popen(finalStatement)
        totalGirls += 1
        # time.sleep(.01)
        # mpv --start=0 425final.mp4 --geometry=1920x1058+0+0 --no-border --video-sync=display-resample --interpolation --keepaspect=no --vf lavfi=[crop=0.7*in_w:0.87*in_h:y=0]
def script():
    newSFM = {}
    if not testMode:
        for key, v in systemFileMap.items():
            if masterType in v and str(v[masterType]) != '0':
                newSFM.setdefault(key, {}).update({masterType: v[masterType]})
            if "E" + masterType in v:
                newSFM.setdefault(key, {}).update({"E" + masterType: v["E" + masterType]})
            pplus = P+masterType
            if pplus in v and str(v[pplus]) != '0':
                newSFM.setdefault(key, {}).update({pplus: v[pplus]})
            if "E" + pplus in v:
                newSFM.setdefault(key, {}).update({"E" + pplus: v["E" + pplus]})
        print(newSFM)
        for girl in range(0,cycledGirls-1):
            for currentGirls in range(0,totalGirls):
                if len(newSFM) == 0:
                    break
                filename, newStartTime = newSFM.popitem()
                print(filename, newStartTime)
                if masterType == 'CSP':
                    cspStartTime = datetime.datetime.strptime(newStartTime[masterType],"%M:%S") - datetime.timedelta(seconds=3)
                    newStartTime[masterType] = cspStartTime.strftime("%M:%S")       
                print(girl)
                if "E" + masterType not in newStartTime and P + masterType not in newStartTime:
                    newStartTime["videoTime"] = defaultDurationMap[masterType]
                else:
                    newStartTime["videoTime"] = getDurationOfSession(newStartTime["E" + masterType], newStartTime[masterType])
                    if masterType in [M, D]:
                        if P+masterType in newStartTime:
                            newStartTime[B + masterType] = getDurationOfSession(newStartTime[P+masterType], newStartTime[masterType])
                            newStartTime[B + P + masterType] = getDurationOfSession(newStartTime["E"+P+masterType], newStartTime[P+masterType])
                            newStartTime[B + "E" + P + masterType] = getDurationOfSession(newStartTime["E"+masterType], newStartTime["E"+P+masterType])
                girlSocketMap[currentGirls].append({filename: newStartTime})
        print(girlSocketMap)
        with open('girlSocketMap.txt', 'w') as f:
            f.write(str(girlSocketMap))

    if not testMode:
        for i in range(1, int(totalTime / intervalCycle)):
            sleep_better(intervalCycle - .002)
            cycles = int(totalTime / intervalCycle)
            timer = time.time()
            for j in range(0, totalGirls):
                socket = win32file.CreateFile(
                        fr'\\.\pipe\mpvsocket{j}',
                        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                        win32file.FILE_SHARE_DELETE | win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                        None,
                        win32file.OPEN_EXISTING,
                        0,
                        None
                    )
                key = next(iter(girlSocketMap[j][i % len(girlSocketMap[j])]))
                girl = girlSocketMap[j][i % len(girlSocketMap[j])][key]
                # print (masterType not in [M, D] or P + masterType not in girl, masterType, P + masterType)
                povPercentage = 0
                fCycle = (100 - povPercentage) / 2
                if masterType not in [M, D] or P + masterType not in girl:
                    videoTime = girl["videoTime"]
                    vidType = girl[masterType]
                else:
                    print (girl)
                    if i + 1 >= fCycle * cycles and i < (fCycle + povPercentage) * cycles:
                        print("phase BP")
                        videoTime = girl[B + P + masterType]
                        vidType = girl[P + masterType] 
                    elif i + 1 >= .66 * cycles:
                        print("phase BPE")
                        if girl[B + "E" + P + masterType] > 0:
                            videoTime = girl[B + "E" + P + masterType]
                            vidType = girl["E" + P + masterType]
                        else:
                            videoTime = girl[B + masterType]
                            vidType = girl[masterType]
                    else:
                        print("phase B")
                        videoTime = girl[B + masterType]
                        vidType = girl[masterType]
                print("girl: ", key)
                if i % len(girlSocketMap[j]) < (totalTime / intervalCycle) % len(girlSocketMap[j]):
                    timesShown = math.ceil((totalTime / intervalCycle) / len(girlSocketMap[j]))
                else:
                    timesShown = math.floor((totalTime / intervalCycle) / len(girlSocketMap[j]))
                endCycle = max(1,int(videoTime / timesShown))
                baseModulo = videoTime % (endCycle)
                startTime = max(0, endCycle * (int((i)/len(girlSocketMap[j])) + 1) - intervalCycle + baseModulo)
                # 18:34 + 87 - 7 + 29
                startTimeConv = datetime.datetime.strptime(vidType,"%M:%S") + datetime.timedelta(seconds=startTime)
                startTimeConv = startTimeConv.strftime("%M:%S")
                print(f"{endCycle=}, {startTime=}, {startTime + intervalCycle=}, {baseModulo=}, {startTimeConv=}, {videoTime=}, {totalTime=}, {intervalCycle=}, {i=}, {j=}, {len(girlSocketMap[j])=}")
                # endCycle = str(datetime.datetime.timedelta(seconds=()))
                loadfileString = f'loadfile {key} replace "start={startTimeConv}" \n'
                win32file.WriteFile(socket, bytes(loadfileString, encoding='utf-8'))
                if startTime + intervalCycle > videoTime:
                    print("STOP THE PRESSES!!!!!!!! THE INTERVAL CYCLE WAS GREATER THAN VIDEOTIME FOR SOME FUCKING REASON")
                # Remember, the reason why we need to base all times off the starting (so if you're videoTime is 210, and your endCycle interval is by 54, is because we start at 0. So if its like 0, 54, 108, 160 etc, we can't just do 0, then 108, etc uknowwhatimean)
            timer2 = time.time()
            print(timer2 - timer)

main()
# print(getDurationOfSession("53:42", "35:49"))