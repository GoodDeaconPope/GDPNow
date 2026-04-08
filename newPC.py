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
import datetime
import win32pipe, win32file, pywintypes
import glob
from pynput.keyboard import Key, Listener
import os 
import threading
import win32api
import win32con
from screeninfo import get_monitors

def get_all_monitors():
    """Get information about all connected monitors."""
    monitors = get_monitors()
    return [{
        'name': f'Monitor {i+1}',
        'x': monitor.x,
        'y': monitor.y,
        'width': monitor.width,
        'height': monitor.height,
        'is_primary': monitor.is_primary
    } for i, monitor in enumerate(monitors)]

def select_monitor():
    """Allow user to select a monitor and return its geometry."""
    monitors = get_all_monitors()
    
    print("\nAvailable Monitors:")
    for i, monitor in enumerate(monitors):
        primary_str = " (Primary)" if monitor['is_primary'] else ""
        print(f"{i+1}. {monitor['name']}{primary_str}")
        print(f"   Resolution: {monitor['width']}x{monitor['height']}")
        print(f"   Position: ({monitor['x']}, {monitor['y']})")
    
    while True:
        try:
            choice = int(input("\nSelect monitor number (1-{}): ".format(len(monitors))))
            if 1 <= choice <= len(monitors):
                selected = monitors[choice-1]
                return [selected['width'], selected['height'], selected['x'], selected['y']]
        except ValueError:
            pass
        print("Invalid selection. Please try again.")

class VideoPlayerConfig:
    def __init__(self, master_type, column_grid, row_grid, cycled_persons, interval_cycle, total_time,
                 input_geometry, pixel_drop, crop_height, crop_width, production, test_mode, monitor_offset):
        self.master_type = master_type
        self.column_grid = column_grid
        self.row_grid = row_grid
        self.cycled_persons = cycled_persons
        self.interval_cycle = interval_cycle
        self.total_time = total_time
        self.input_geometry = input_geometry
        self.pixel_drop = pixel_drop
        self.crop_height = crop_height
        self.crop_width = crop_width
        self.production = production
        self.test_mode = test_mode
        self.monitor_offset = monitor_offset
        
        # Performance settings - change these manually as needed
        self.use_interpolation = True  # Set to False to reduce GPU load
        self.optimize_performance = True  # Set to True to enable basic optimizations
        self.extra_optimization = True  # Set to True for additional optimizations
        self.lower_resolution = True  # Set to True to reduce resolution

        self.pixel_drop_map = {
            "PC": 50, "RC": 100, "PM": 0, "D": 100, "M": 130, "CSP": 200
        }
        self.default_duration_map = {
            "PC": 60, "M": 360, "RC": 360, "PM": 60, "D": 360, "PD": 60,
            "CP": interval_cycle, "CSP": interval_cycle, "CS": interval_cycle,
            "B": 180, "BP": 60, "BEP": 180
        }

def create_mpv_command(video_file, start_time, geometry, config):
    # Base parameters
    pc_parameters = f"--no-border --video-sync=display-resample --keepaspect --no-keepaspect-window --panscan=1 "
    
    # Only add interpolation if enabled
    if config.use_interpolation:
        pc_parameters += "--interpolation "

    if config.column_grid * config.row_grid >= 9:  # For 5x5 grid
        pc_parameters += "--vd-lavc-skiploopfilter=all --vd-lavc-fast "
    
    # Performance optimization parameters (only if enabled)
    performance_params = ""
    if config.optimize_performance:
        performance_params = "--scale=bilinear --cscale=bilinear --dscale=bilinear --hwdec=auto-copy --vd-lavc-threads=4 "
        
        # Add additional optimizations if enabled
        if config.extra_optimization:
            performance_params += "--vo=gpu --cache=yes --demuxer-max-bytes=100M --vd-lavc-skiploopfilter=nonkey "
        
        # Add resolution scaling if enabled
        if config.lower_resolution:
            scale_width = math.ceil(config.input_geometry[0] / config.column_grid)
            scale_height = math.ceil(config.input_geometry[1] / config.row_grid)
            scale_param = f"--vf-add=scale={scale_width}:{scale_height}"
        else:
            scale_param = ""
    else:
        performance_params = ""
        scale_param = ""

    # Keep the original lavfi cropping parameter
    lavfi = f"crop={config.crop_width}*in_w:{config.crop_height}*in_h:y=0"
    ffmpeg_params = f"--vf=lavfi=[{lavfi}] {scale_param}"
    if config.test_mode:
        pc_parameters += "--no-video --no-audio "
    
    if config.master_type == "PC":
        pc_parameters += "--brightness=6 --gamma=7 "
    
    # For single video mode, add fullscreen flag
    if config.column_grid == 1 and config.row_grid == 1:
        pc_parameters += "--fs "
    
    socket_params = f"--input-ipc-server=mpvsocket{len(sockets)} "
    
    # Keep the existing lavfi cropping parameter

    
    command = f"mpv --start={start_time} {video_file} --geometry={geometry} {pc_parameters} {performance_params} {socket_params} {ffmpeg_params}"
    print(command)
    return shlex.split(command)
def start_mpv_instance(command):
    return subprocess.Popen(command)

def create_socket(index, retries=100000, delay=0):
    pipe_name = fr'\\.\pipe\mpvsocket{index}'
    for attempt in range(retries):
        try:
            return win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,  # No sharing
                None,  # Default security attributes
                win32file.OPEN_EXISTING,
                win32file.FILE_FLAG_OVERLAPPED,  # Use overlapped I/O for better performance
                None  # No template file
            )
        except pywintypes.error as e:
            if e.args[0] == 2:  # ERROR_FILE_NOT_FOUND
                if attempt % 100 == 0:  # Only log occasionally to reduce console spam
                    print(f"Attempt {attempt + 1}/{retries} - Pipe not found: {pipe_name}")
                time.sleep(delay)
            else:
                raise
    raise FileNotFoundError(f"Failed to create socket after {retries} attempts: {pipe_name}")

sockets = []
processes = []
interrupt_event = threading.Event()
person_socket_map = {}

def play_videos(config, video_list):
    global sockets, processes, person_socket_map
    
    # Filter the video list to only include videos with the required start_time
    valid_videos = [(filename, start_time) for filename, start_time in video_list if config.master_type in start_time]

    # Ensure there are enough videos for the required cycled_persons
    required_videos_count = config.column_grid * config.row_grid * config.cycled_persons
    valid_videos = valid_videos[:required_videos_count]

    for i in range(config.column_grid * config.row_grid):
        person_socket_map[i] = []

    # Distribute the valid videos among the sockets
    for idx, (filename, start_time) in enumerate(valid_videos):
        socket_index = idx % (config.column_grid * config.row_grid)
        person_socket_map[socket_index].append({filename: start_time})
    
    for i in range(config.column_grid * config.row_grid):
        geometry = calculate_geometry(i, config)
        print(geometry, i)
        
        first_video = person_socket_map[i][0]
        first_filename = list(first_video.keys())[0]
        first_start_time = first_video[first_filename][config.master_type]

        command = create_mpv_command(first_filename, first_start_time, geometry, config)
        if config.production:
            processes.append(start_mpv_instance(command))
        sockets.append(create_socket(i))
        
        if not config.test_mode:
            for video in person_socket_map[i]:
                filename, start_time = list(video.items())[0]
                if "E" + config.master_type not in start_time:
                    start_time["videoTime"] = config.default_duration_map[config.master_type]
                else:
                    start_time["videoTime"] = get_duration_of_session(start_time["E" + config.master_type], start_time[config.master_type])
                    if config.master_type in ["M", "D"]:
                        if "P" + config.master_type in start_time:
                            start_time["B" + config.master_type] = get_duration_of_session(start_time["P" + config.master_type], start_time[config.master_type])
                            start_time["BP" + config.master_type] = get_duration_of_session(start_time["E" + "P" + config.master_type], start_time["P" + config.master_type])
                            start_time["BE" + "P" + config.master_type] = get_duration_of_session(start_time["E" + config.master_type], start_time["E" + "P" + config.master_type])
    
    print(person_socket_map)
    
    if config.cycled_persons > 1:
        cycle_thread = threading.Thread(target=cycle_videos, args=(config, video_list))
        cycle_thread.start()
        
        with Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()
        
        cycle_thread.join()
    else:
        for process in processes:
            process.wait()

def cycle_videos(config, video_list):
    cycles = int(config.total_time / config.interval_cycle)
    
    # Pre-prepare all commands for each cycle to reduce processing during the cycle
    cycle_commands = []
    for i in range(1, cycles):
        cycle_data = []
        for j in range(len(sockets)):
            if j < len(person_socket_map) and i % len(person_socket_map[j]) < len(person_socket_map[j]):
                person_info = person_socket_map[j][i % len(person_socket_map[j])]
                filename = list(person_info.keys())[0]
                person = person_info[filename]
                
                pov_percentage = 0
                f_cycle = (100 - pov_percentage) / 2
                
                if config.master_type not in ["M", "D"] or "P" + config.master_type not in person:
                    video_time = person["videoTime"]
                    vid_type = person[config.master_type]
                else:
                    if i + 1 >= f_cycle * cycles and i < (f_cycle + pov_percentage) * cycles:
                        video_time = person["BP" + config.master_type]
                        vid_type = person["P" + config.master_type]
                    elif i + 1 >= 0.66 * cycles:
                        if person["BE" + "P" + config.master_type] > 0:
                            video_time = person["BE" + "P" + config.master_type]
                            vid_type = person["E" + "P" + config.master_type]
                        else:
                            video_time = person["B" + config.master_type]
                            vid_type = person[config.master_type]
                    else:
                        video_time = person["B" + config.master_type]
                        vid_type = person[config.master_type]
                
                times_shown = math.ceil((config.total_time / config.interval_cycle) / len(person_socket_map[j])) if i % len(person_socket_map[j]) < (config.total_time / config.interval_cycle) % len(person_socket_map[j]) else math.floor((config.total_time / config.interval_cycle) / len(person_socket_map[j]))
                end_cycle = max(1, int(video_time / times_shown))
                base_modulo = video_time % end_cycle
                start_time = max(0, end_cycle * (int(i / len(person_socket_map[j])) + 1) - config.interval_cycle + base_modulo)
                
                start_time_conv = (datetime.datetime.strptime(vid_type, "%M:%S") + datetime.timedelta(seconds=start_time)).strftime("%M:%S")
                
                loadfile_command = f'loadfile {filename} replace "start={start_time_conv}" \n'
                cycle_data.append((j, loadfile_command))
        
        cycle_commands.append(cycle_data)
    
    # Create overlapped I/O objects for each socket
    overlapped_ios = [pywintypes.OVERLAPPED() for _ in range(len(sockets))]
    
    for i, cycle_data in enumerate(cycle_commands):
        if interrupt_event.is_set():
            break
            
        # Sleep until next cycle time
        time.sleep(config.interval_cycle - 0.05)  # Slightly shorter sleep to prepare commands
        
        # Send all commands nearly simultaneously using overlapped I/O
        pending_operations = []
        
        for j, command in cycle_data:
            try:
                # Start async write operation
                err, _ = win32file.WriteFile(sockets[j], command.encode('utf-8'), overlapped_ios[j])
                if err != 0 and err != win32file.ERROR_IO_PENDING:
                    print(f"Error starting write to socket {j}: {err}")
                else:
                    pending_operations.append((j, overlapped_ios[j]))
            except pywintypes.error as e:
                print(f"Exception writing to socket {j}: {e}")
        
        # Wait for all operations to complete
        for j, overlapped in pending_operations:
            try:
                win32file.GetOverlappedResult(sockets[j], overlapped, True)
            except pywintypes.error as e:
                print(f"Error completing write to socket {j}: {e}")

def on_press(key):
    if hasattr(key, 'char'):
        if key.char == 'q':
            interrupt_event.set()
            quit_command = b'{"command": ["quit"]}\n'
            for socket in sockets:
                try:
                    win32file.WriteFile(socket, quit_command)
                except pywintypes.error:
                    pass
            time.sleep(0.5)
            for process in processes:
                process.terminate()
            os._exit(1)

def on_release(key):
    if key == Key.esc:
        return False

def calculate_geometry(index, config):
    input_width, input_height = config.input_geometry[:2]
    output_width = math.ceil(input_width / config.column_grid)
    output_height = math.ceil(input_height / config.row_grid)
    
    col = index % config.column_grid
    row = index // config.column_grid
    
    # Add monitor offset to position
    width_position = output_width * col + config.monitor_offset[0]
    height_position = output_height * row + config.monitor_offset[1]
    
    return f"{output_width}x{output_height}+{width_position}+{height_position}"

def prepare_video_list():
    with open('systemFileMap.json') as d:
        system_file_map = json.load(d)
    
    all_files = glob.glob('*.mp4')
    system_file_map = {k: v for k, v in system_file_map.items() if k in all_files}
    
    # Create a list for selected files from arguments
    args_file_map = {}
    for arg in sys.argv[1:]:
        for key in list(system_file_map.keys()):
            if arg in key:
                args_file_map[key] = system_file_map.pop(key)
                break
    
    # Shuffle the remaining files
    shuffled_items = list(system_file_map.items())
    random.shuffle(shuffled_items)
    shuffled_system_file_map = dict(shuffled_items)
    
    # Create final list with argument files first, then shuffled files
    final_list = list(args_file_map.items()) + list(shuffled_system_file_map.items())
    
    return final_list

def get_duration_of_session(time1, time2):
    duration = datetime.datetime.strptime(time1, "%M:%S") - datetime.datetime.strptime(time2, "%M:%S")
    return int(duration.total_seconds())

def main():
    # Get monitor selection from user
    monitor_geometry = select_monitor()
    input_geometry = monitor_geometry[:2]  # width, height
    monitor_offset = monitor_geometry[2:]  # x, y offset
    
    master_type = input("What SP do you want? LMFAO: ").upper() or "RC"
    # Allow override of detected monitor resolution
    input_geometry[0] = int(input(f"What is your X screen size ({input_geometry[0]}): ") or input_geometry[0])
    input_geometry[1] = int(input(f"What is your Y screen size ({input_geometry[1]}): ") or input_geometry[1])
    column_grid = int(input("Horizontal Videos: ") or 1)
    row_grid = int(input("Vertical Videos: ") or 1)
    cycled_persons = int(input("Number of persons to cycle through ") or 1)
    
    interval_cycle = 15
    total_time = 300
    if cycled_persons > 1:
        if master_type != 'CSP':
            interval_cycle = int(input("How long do you wanna watch each person before switching to another? ") or 15)
            total_time = int(input("How long do you think your session will be? ") or 300)
    if master_type == 'CSP':
        interval_cycle = 28
        total_time = interval_cycle * cycled_persons
    
    pixel_drop = {"PC": 50, "RC": 100, "PM": 0, "D": 100, "M": 130, "CSP": 200}.get(master_type, 100)
    crop_height = 0.88
    calculated_value = ((input_geometry[0] / column_grid) / ((input_geometry[1] / row_grid) + pixel_drop) * (input_geometry[1] * crop_height)) / input_geometry[0]
    print(calculated_value)
    crop_width = {"PC": calculated_value, "M": calculated_value, "RC": calculated_value, "PM": 1, "D": calculated_value, "CSP": calculated_value}.get(master_type, 0.8)
    
    video_list = prepare_video_list()
    config = VideoPlayerConfig(
        master_type=master_type,
        column_grid=column_grid,
        row_grid=row_grid,
        cycled_persons=cycled_persons,
        interval_cycle=interval_cycle,
        total_time=total_time,
        input_geometry=input_geometry,
        pixel_drop=pixel_drop,
        crop_height=crop_height,
        crop_width=crop_width,
        production=True,
        test_mode=False,
        monitor_offset=monitor_offset  # Add monitor offset to config
    )
    
    play_videos(config, video_list)
if __name__ == "__main__":
    main()