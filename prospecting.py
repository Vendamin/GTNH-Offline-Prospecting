import csv
import io
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import time
import argparse
import multiprocessing
import threading
from queue import Empty

import anvil

SCRIPT_DIR = Path(__file__).resolve().parent

parser = argparse.ArgumentParser(description="Offline Prospecting Tool for GTNH")
parser.add_argument("--region-dir", type=str, default=str(SCRIPT_DIR/"test"), help="Path to the region directory")
parser.add_argument("--regions", type=str, default="", help="Comma-separated list of region files to process (optional)")
parser.add_argument("--dump-dir", type=str, default=str(SCRIPT_DIR/"dumps"), help="Path to the dump directory")
parser.add_argument("--export-dir", type=str, default=str(SCRIPT_DIR/"data"), help="Path to the export directory")
parser.add_argument("--cpu-usage", type=float, default=0.75, help="Fraction of CPU to use (0.0 to 1.0)")
parser.add_argument("--skip-block-ids", type=str, default="0,7,1,2,3,9,12,13,24,80,11,87,88,1202,121,1379,1418", help="Comma-separated list of block IDs to skip")
parser.add_argument("--only-ores", action="store_true", help="Only process ores (skip non-ore blocks)")
args = parser.parse_args()

CPU_USAGE_FRACTION = max(0.0, min(1.0, args.cpu_usage))
REGION_DIR = Path(args.region_dir) if args.region_dir else None
DUMP_DIR = Path(args.dump_dir) if args.dump_dir else None
EXPORT_DIR = Path(args.export_dir) if args.export_dir else None
REGION_FILES = [Path(r.strip()) for r in args.regions.split(",") if r.strip()] if args.regions else None

if not DUMP_DIR or not REGION_DIR or not EXPORT_DIR:
    raise ValueError("DUMP_DIR, REGION_DIR, and EXPORT_DIR must be specified.")

EXPORT_DIR.mkdir(parents=True, exist_ok=True)

W_BLOCK_LOOKUP = {}
W_OREDICT_LOOKUP = {}
W_ORES_IDS = set()
W_SKIPPED_IDS = set()
W_ONLY_ORES = False
W_CACHE = {} 

def is_ore_registry_name(registry_name):
    if not registry_name: return False
    rn = str(registry_name).lower()
    return "blockores" in rn or "_ore" in rn or rn.endswith("ore")

def init_worker(data_dir, skipped_ids_str, only_ores):
    global W_BLOCK_LOOKUP, W_OREDICT_LOOKUP, W_ORES_IDS, W_SKIPPED_IDS, W_ONLY_ORES
    
    W_ONLY_ORES = only_ores
    W_SKIPPED_IDS = set(int(x.strip()) for x in skipped_ids_str.split(",")) if skipped_ids_str else set()
    W_SKIPPED_IDS.add(0)
    
    W_BLOCK_LOOKUP = load_block_lookup(data_dir)
    W_OREDICT_LOOKUP = load_oredict_lookup(data_dir)
    
    W_ORES_IDS = set()
    for b_id, info in W_BLOCK_LOOKUP.items():
        reg_name = normalize_identifier(info.get("name"))
        if is_ore_registry_name(reg_name):
            W_ORES_IDS.add(b_id)

def read_nibble(byte_array, index):
    value = byte_array[index // 2]
    if value < 0: value += 256
    return value & 0x0F if index % 2 == 0 else (value >> 4) & 0x0F

def read_u16_be(byte_array, index):
    first = byte_array[index * 2]
    second = byte_array[index * 2 + 1]
    if first < 0: first += 256
    if second < 0: second += 256
    return (first << 8) | second

def normalize_identifier(value):
    if not value: return None
    text = str(value).strip()
    return None if not text or text.lower() == "null" else text.lower()

def parse_optional_int(value):
    if not value: return None
    text = str(value).strip()
    if not text or text.lower() == "null": return None
    try: return int(text)
    except ValueError: return None

def parse_itemstack_meta(itemstack_value):
    if not itemstack_value or "@" not in str(itemstack_value): return None
    return parse_optional_int(str(itemstack_value).rsplit("@", 1)[1].strip())

def parse_itemstack_identifier(itemstack_value):
    if not itemstack_value: return None
    text = str(itemstack_value).strip()
    if "@" in text: text = text.rsplit("@", 1)[0].strip()
    if text.startswith("1x"): text = text[2:].strip()
    return normalize_identifier(text)

def find_data_dir():
    candidate_dirs = [DUMP_DIR, SCRIPT_DIR]
    for c_dir in candidate_dirs:
        if c_dir and (c_dir / "oredict.csv").exists() and (c_dir / "block.csv").exists():
            return c_dir
    raise FileNotFoundError("Could not find 'oredict.csv' and 'block.csv'")

def open_dump_csv(path):
    # NEI writes dumps in the platform default encoding (cp1252 on Windows), so § color codes aren't valid UTF-8
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return io.StringIO(raw.decode(encoding), newline="")
        except UnicodeDecodeError:
            continue
    return io.StringIO(raw.decode("latin-1"), newline="")

def load_block_lookup(data_dir):
    lookup = {}
    with open_dump_csv(data_dir / "block.csv") as h:
        for row in csv.DictReader(h):
            b_id = parse_optional_int(row.get("ID"))
            if b_id is not None:
                lookup[b_id] = {"name": row.get("Name", "").strip(), "display_name": row.get("Display Name", "").strip(), "id": b_id}
    return lookup

def load_oredict_lookup(data_dir):
    lookup = {}
    with open_dump_csv(data_dir / "oredict.csv") as h:
        for row in csv.DictReader(h):
            i_id = normalize_identifier(row.get("Item ID"))
            is_id = parse_itemstack_identifier(row.get("ItemStack"))
            meta = parse_itemstack_meta(row.get("ItemStack"))
            d_name = row.get("Display Name", "").strip()
            if not i_id or meta is None or not d_name: continue
            for lookup_id in {i_id, is_id}:
                if lookup_id: lookup[(lookup_id, meta)] = d_name
    return lookup

def decode_section_block(section, index):
    tag_names = {tag.name for tag in section.tags}
    if "Blocks16" in tag_names:
        return read_u16_be(section["Blocks16"].value, index), (read_u16_be(section["Data16"].value, index) if "Data16" in tag_names else 0), False
    block_id = section["Blocks"].value[index]
    if block_id < 0: block_id += 256
    if "Add" in tag_names: block_id += read_nibble(section["Add"].value, index) << 8
    if "BlocksB2Hi" in tag_names: block_id += read_nibble(section["BlocksB2Hi"].value, index) << 12
    data_low = read_nibble(section["Data"].value, index) if "Data" in tag_names else 0
    data_high = read_nibble(section["Data1High"].value, index) if "Data1High" in tag_names else 0
    data_high_byte = section["Data2"].value[index] if "Data2" in tag_names else 0
    if data_high_byte < 0: data_high_byte += 256
    return block_id, ((data_high_byte << 8) | (data_high << 4) | data_low), bool(data_high_byte & 0x40)

def iter_ore_meta_candidates(block_meta):
    if block_meta is None: return []
    candidates = []
    for cand in (block_meta, block_meta % 10000, block_meta % 1000):
        if cand not in candidates: candidates.append(cand)
    return candidates

def resolve_block_name_and_meta(block_id, block_meta):
    cache_key = (block_id, block_meta)
    if cache_key in W_CACHE: return W_CACHE[cache_key]

    block_info = W_BLOCK_LOOKUP.get(block_id)
    registry_name = normalize_identifier(block_info.get("name")) if block_info else None
    
    if not registry_name:
        try:
            b = anvil.chunk.Block.from_numeric_id(block_id, block_meta)
            registry_name = normalize_identifier(b.name)
        except Exception: pass

    if registry_name:
        meta_cands = iter_ore_meta_candidates(block_meta) if is_ore_registry_name(registry_name) else [block_meta]
        for mc in meta_cands:
            exact = W_OREDICT_LOOKUP.get((registry_name, mc))
            if exact:
                res = (exact, mc)
                W_CACHE[cache_key] = res
                return res

    if block_info:
        d_name = block_info.get("display_name")
        if d_name and d_name != "null":
            if d_name == "Ore": d_name = "Undiscovered ore"
            res = (d_name, block_meta)
            W_CACHE[cache_key] = res
            return res
        if block_info.get("name"):
            res = (block_info.get("name"), block_meta)
            W_CACHE[cache_key] = res
            return res

    res = ("Unknown name", block_meta)
    W_CACHE[cache_key] = res
    return res

def check_tile_entity_ores(chunk_nbt):
    level = chunk_nbt["Level"]
    try:
        tile_entities = level["TileEntities"]
        for te in tile_entities:
            try:
                te_id = normalize_identifier(te["id"].value)
                if te_id == normalize_identifier("GT_TileEntity_Ores"): return True
            except Exception: continue
        return False
    except Exception: return False

def process_and_save_region(file_path, export_dir_str, progress_queue):
    export_dir = Path(export_dir_str)
    region_name = os.path.basename(file_path)
    region_base = os.path.splitext(region_name)[0]
    
    try: region = anvil.Region.from_file(file_path)
    except Exception: return region_name, 0, 0.0, {}

    region_data = {}
    local_dictionary = {}
    chunk_count = 0
    time_start = time.time()

    try:
        universal_ore_id = 0
        for block in W_BLOCK_LOOKUP.values():
            if normalize_identifier(block.get("display_name")) == "gt.blockores.0.name":
                universal_ore_id = block.get("id", "")
                break
    except Exception: pass

    for region_chunk_x in range(32):
        for region_chunk_z in range(32):
            try: chunk_nbt = region.chunk_data(region_chunk_x, region_chunk_z)
            except Exception: continue
            if chunk_nbt is None: continue

            TileEntityOres = check_tile_entity_ores(chunk_nbt)

            level = chunk_nbt["Level"]
            world_chunk_x = level["xPos"].value
            world_chunk_z = level["zPos"].value
            chunk_blocks = []

            if TileEntityOres:
                tile_entities = level["TileEntities"]
                for te in tile_entities:
                    try:
                        te_id = normalize_identifier(te["id"].value)
                        if te_id == normalize_identifier("GT_TileEntity_Ores"): 
                            block_id = universal_ore_id
                            block_data = parse_optional_int(te.get("m"))
                            small = str(te.get("m", "")).isdigit() and len(str(te.get("m"))) > 4
                            b_name, b_meta = resolve_block_name_and_meta(block_id, block_data)
                            if b_name == "gt.blockores.0.name": continue
                            if b_name == "minecraft:air": continue
                            
                            dict_key = f"{block_id}:{b_meta}:{int(small)}"
                            if dict_key not in local_dictionary:
                                local_dictionary[dict_key] = b_name
                            
                            abs_x = te["x"].value
                            abs_y = te["y"].value
                            abs_z = te["z"].value
                            chunk_blocks.append([abs_x, abs_y, abs_z, block_id, b_meta, int(small)])
                    except Exception: continue
            
            if (not TileEntityOres or not W_ONLY_ORES or True):
                sections = level["Sections"] if "Sections" in [t.name for t in level.tags] else []

                for section in sections:
                    section_min_y = section["Y"].value * 16
                    for local_y in range(16):
                        for local_z in range(16):
                            for local_x in range(16):
                                index = (local_y << 8) | (local_z << 4) | local_x
                                block_id, block_data, small = decode_section_block(section, index)

                                if W_ONLY_ORES and block_id not in W_ORES_IDS: continue
                                if block_id in W_SKIPPED_IDS: continue

                                b_name, b_meta = resolve_block_name_and_meta(block_id, block_data)
                                if b_name == "gt.blockores.0.name": continue
                                if b_name == "minecraft:air": continue
                                if small and not b_name.startswith("Small"): b_name = f"Small {b_name}"
                                
                                dict_key = f"{block_id}:{b_meta}:{int(small)}"
                                if dict_key not in local_dictionary:
                                    local_dictionary[dict_key] = b_name
                                
                                abs_x = world_chunk_x * 16 + local_x
                                abs_y = section_min_y + local_y
                                abs_z = world_chunk_z * 16 + local_z
                                chunk_blocks.append([abs_x, abs_y, abs_z, block_id, b_meta, int(small)])
            
            if chunk_blocks: region_data[f"{world_chunk_x},{world_chunk_z}"] = chunk_blocks
            chunk_count += 1
            if progress_queue and chunk_count % 32 == 0: progress_queue.put(32)

    if progress_queue and chunk_count % 32 != 0: progress_queue.put(chunk_count % 32)
    time_spent = time.time() - time_start

    if region_data:
        output_file = export_dir / f"{region_base}.json"
        with output_file.open("w", encoding="utf-8") as f:
            json.dump(region_data, f, ensure_ascii=False, separators=(',', ':'))

    return region_name, chunk_count, time_spent, local_dictionary


def get_chunk_count(file_path):
    region = anvil.Region.from_file(file_path)
    count = 0
    for x in range(32):
        for z in range(32):
            try:
                if region.chunk_data(x, z) is not None: count += 1
            except Exception: pass
    return count

def print_progress_live(completed, total, time_per_chunk, done_chunks, total_chunks):
    if total_chunks == 0: return
    width = 30
    ratio = done_chunks / total_chunks
    if ratio > 1.0: ratio = 1.0 
    
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    
    rem_time = max(0, (total_chunks - done_chunks)) * time_per_chunk
    if rem_time < 60: rem_str = f"{rem_time:.1f} s"
    elif rem_time < 3600: rem_str = f"{rem_time / 60:.1f} min"
    elif rem_time < 86400: rem_str = f"{rem_time / 3600:.1f} h"
    else: rem_str = f"{rem_time / 86400:.1f} d"
    
    print(f"Progress: [{bar}] Regions: {completed}/{total} | Chunks: {done_chunks}/{total_chunks} ({ratio * 100:.1f}%) | ETA: {rem_str}", end="\r", flush=True)


def main():
    data_dir = find_data_dir()
    if REGION_FILES is not None:
        target_names = {rf.name for rf in REGION_FILES}
        region_files = [str(p) for p in REGION_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".mca" and p.name in target_names]
    else:
        region_files = [str(p) for p in REGION_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".mca"]

    total_regions = len(region_files)
    cpu_count = os.cpu_count() or 1
    max_workers = max(1, int(cpu_count * CPU_USAGE_FRACTION))

    print(f"Found {total_regions} region files. Target CPU: {max_workers}/{cpu_count} workers.")

    total_chunks = 0
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for count in executor.map(get_chunk_count, region_files):
            total_chunks += count

    with multiprocessing.Manager() as manager:
        progress_queue = manager.Queue()
        completed_regions = [0]
        stop_monitor = [False]
        
        def progress_monitor():
            done_chunks = 0
            start_time = time.time()
            while not stop_monitor[0] or not progress_queue.empty():
                try:
                    chunks_done = progress_queue.get(timeout=0.2)
                    done_chunks += chunks_done
                    elapsed = time.time() - start_time
                    time_per_chunk = elapsed / done_chunks if done_chunks else 0
                    print_progress_live(completed_regions[0], total_regions, time_per_chunk, done_chunks, total_chunks)
                except Empty:
                    pass
            print_progress_live(total_regions, total_regions, 0, total_chunks, total_chunks)
            print() 
        
        monitor_thread = threading.Thread(target=progress_monitor)
        monitor_thread.start()

        global_dictionary = {} 

        with ProcessPoolExecutor(max_workers=max_workers, initializer=init_worker, initargs=(data_dir, args.skip_block_ids, args.only_ores)) as executor:
            futures = [executor.submit(process_and_save_region, f, str(EXPORT_DIR), progress_queue) for f in region_files]
            for future in as_completed(futures):
                completed_regions[0] += 1
                region_name, chunk_count, time_spent, local_dict = future.result()
                global_dictionary.update(local_dict)

        stop_monitor[0] = True
        monitor_thread.join()

    dict_file = EXPORT_DIR / "dictionary.json"
    if dict_file.exists():
        try:
            with dict_file.open("r", encoding="utf-8") as f:
                existing_dict = json.load(f)
                existing_dict.update(global_dictionary)
                global_dictionary = existing_dict
        except Exception:
            pass 

    with dict_file.open("w", encoding="utf-8") as f:
        json.dump(global_dictionary, f, ensure_ascii=False, indent=4)
    
    print("\nDone. All JSON files and dictionary exported successfully.")

if __name__ == "__main__":
    main()
