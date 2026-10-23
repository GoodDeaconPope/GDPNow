import json

def parse_timestamp(ts):
    """Convert MM:SS to total seconds"""
    if not ts:
        return None
    parts = ts.split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:  # HH:MM:SS
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return None

def format_duration(seconds):
    """Format seconds as HH:MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def calculate_duration(data, start_key, end_key):
    """Calculate duration between start and end timestamps"""
    start = parse_timestamp(data.get(start_key))
    end = parse_timestamp(data.get(end_key))
    if start is not None and end is not None:
        return end - start
    return 0

# Load the JSON file
with open('E:/1SystemFile/systemFileMap.json', 'r') as f:
    file_map = json.load(f)

# Define scene types and their sub-scenes
# Main scenes: start_key -> end_key
main_scenes = {
    'M': 'EM',    # Main
    'D': 'ED',    # D
    'RC': 'ERC',  # RC
    'PC': 'EPC',  # PC
    'BJ': 'EBJ', # BJ
}

# Sub-scenes (these are inside parent scenes)
sub_scenes = {
    'PM': ('EPM', 'M'),   # PM is inside M
    'PD': ('EPD', 'D'),   # PD is inside D
}

# Stats storage
stats = {scene: {'total': 0, 'count': 0, 'files': []} for scene in main_scenes}
sub_stats = {scene: {'total': 0, 'count': 0, 'files': []} for scene in sub_scenes}

# Process each file
for filename, timestamps in file_map.items():
    # Calculate main scene durations
    for start_key, end_key in main_scenes.items():
        duration = calculate_duration(timestamps, start_key, end_key)
        if duration > 0:
            stats[start_key]['total'] += duration
            stats[start_key]['count'] += 1
            stats[start_key]['files'].append((filename, duration))

    # Calculate sub-scene durations
    for start_key, (end_key, parent) in sub_scenes.items():
        duration = calculate_duration(timestamps, start_key, end_key)
        if duration > 0:
            sub_stats[start_key]['total'] += duration
            sub_stats[start_key]['count'] += 1
            sub_stats[start_key]['files'].append((filename, duration))

# Print results
print("=" * 60)
print("MAIN SCENE STATISTICS (Full Duration)")
print("=" * 60)

grand_total = 0
for scene, data in stats.items():
    if data['count'] > 0:
        avg = data['total'] // data['count']
        print(f"\n{scene} -> E{scene}:")
        print(f"  Total time:    {format_duration(data['total'])}")
        print(f"  Count:         {data['count']} videos")
        print(f"  Average:       {format_duration(avg)}")
        grand_total += data['total']

print("\n" + "=" * 60)
print("SUB-SCENE STATISTICS (Already included in parent totals)")
print("=" * 60)

sub_total = 0
for scene, data in sub_stats.items():
    if data['count'] > 0:
        parent = sub_scenes[scene][1]
        avg = data['total'] // data['count']
        print(f"\n{scene} -> E{scene} (sub-scene of {parent}):")
        print(f"  Total time:    {format_duration(data['total'])}")
        print(f"  Count:         {data['count']} videos")
        print(f"  Average:       {format_duration(avg)}")
        sub_total += data['total']

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"\nGrand total (all main scenes):     {format_duration(grand_total)}")
print(f"Sub-scene time (PM + PD):          {format_duration(sub_total)}")
print(f"Adjusted total (excluding subs):   {format_duration(grand_total - sub_total)}")

# Calculate M and D adjusted totals
m_adjusted = stats['M']['total'] - sub_stats['PM']['total']
d_adjusted = stats['D']['total'] - sub_stats['PD']['total']

print(f"\nM time (excluding PM):             {format_duration(m_adjusted)}")
print(f"D time (excluding PD):             {format_duration(d_adjusted)}")

print(f"\nTotal videos analyzed: {len(file_map)}")
