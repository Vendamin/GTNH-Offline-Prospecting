import os
import json
import subprocess
import signal
import sys
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory

app = Flask(__name__)

SETTINGS_FILE = "web_settings.json"
DEFAULT_SETTINGS = {
    "world_dir": "",
    "skip_block_ids": "7,1,2,3,9,12,13,24,80,11,87,88,1202,121,1379,1418",
    "cpu_usage": 75,
    "only_ores": True,
    "only_ores_and_fluids": False,
    "dump_dir": "dumps",
    "export_dir": "data"
}

RUNNING_PROCESSES = {}

DIMENSIONS = {
    "Vanilla & Core": {
        "overworld": "Overworld (Earth)",
        "DIM-1": "The Nether",
        "DIM1": "The End"
    },
    "Solar System": {
        "DIM28": "Moon",
        "DIM37": "Mercury",
        "DIM39": "Venus",
        "DIM29": "Mars",
        "DIM38": "Phobos",
        "DIM40": "Deimos",
        "DIM30": "Asteroids",
        "DIM42": "Ceres",
        "DIM71": "Jupiter",
        "DIM36": "Io",
        "DIM35": "Europa",
        "DIM43": "Ganymede",
        "DIM45": "Callisto",
        "DIM77": "Saturn",
        "DIM41": "Enceladus",
        "DIM44": "Titan",
        "DIM51": "Uranus",
        "DIM86": "Miranda",
        "DIM46": "Oberon",
        "DIM74": "Neptune",
        "DIM47": "Proteus",
        "DIM48": "Triton",
        "DIM49": "Pluto",
        "DIM83": "Haumea",
        "DIM25": "Makemake",
        "DIM33": "Kuiper Belt"
    },
    "Deep Space & Star Systems": {
        "DIM31": "Alpha Centauri Bb",
        "DIM32": "Barnarda C",
        "DIM81": "Barnarda E",
        "DIM82": "Barnarda F",
        "DIM63": "Ross128ba",
        "DIM64": "Ross128b",
        "DIM84": "Vega B",
        "DIM85": "T Ceti E",
        "DIM90": "Neper",
        "DIM91": "Maahes",
        "DIM92": "Anubis",
        "DIM93": "Horus",
        "DIM94": "Seth",
        "DIM95": "Amun-Ra"
    },
    "Magic & Utility": {
        "DIM7": "Twilight Forest",
        "DIM100": "Deep Dark",
        "DIM112": "The Last Millenium",
        "DIM2": "Spectre World",
        "DIM5": "Storage Cell",
        "DIM50": "The Outer Lands",
        "DIM173": "Outer Lands (DIM173)",
        "DIM55": "Spirit World",
        "DIM56": "The Torment",
        "DIM60": "Bedrock Dimension",
        "DIM69": "Pocket Plane",
        "DIM70": "Mirror",
        "DIM227": "Toxic Everglades"
    }
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = {**DEFAULT_SETTINGS, **json.load(f)}
                if isinstance(settings["cpu_usage"], str) and "." in settings["cpu_usage"]:
                   settings["cpu_usage"] = int(float(settings["cpu_usage"]) * 100)
                elif isinstance(settings["cpu_usage"], float):
                   settings["cpu_usage"] = int(settings["cpu_usage"] * 100)
                return settings
        except Exception:
            return DEFAULT_SETTINGS
    return DEFAULT_SETTINGS

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)

def get_version():
    version_file = "version.txt"
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "Unknown"

@app.route("/")
def index():
    settings = load_settings()
    running = {pid: {"cmd": info["cmd"]} for pid, info in list(RUNNING_PROCESSES.items()) if info["process"].poll() is None}
    for pid in list(RUNNING_PROCESSES.keys()):
        if RUNNING_PROCESSES[pid]["process"].poll() is not None:
            del RUNNING_PROCESSES[pid]
            
    export_dir = settings["export_dir"]
    available_dims = []
    if os.path.exists(export_dir):
        available_dims = [d for d in os.listdir(export_dir) if os.path.isdir(os.path.join(export_dir, d))]
        
    return render_template("index.html", settings=settings, dimensions=DIMENSIONS, running_processes=running, available_dims=available_dims, version=get_version())

@app.route("/save_settings", methods=["POST"])
def save_config():
    try: cpu_val = int(request.form.get("cpu_usage", "75"))
    except ValueError: cpu_val = 75
    
    current_settings = load_settings()
    
    settings = {
        "world_dir": request.form.get("world_dir", "").strip(),
        "skip_block_ids": request.form.get("skip_block_ids", "").strip(),
        "cpu_usage": cpu_val,
        "only_ores": "only_ores" in request.form,
        "only_ores_and_fluids": "only_ores_and_fluids" in request.form,
        "dump_dir": request.form.get("dump_dir", "dumps").strip(),
        "export_dir": current_settings.get("export_dir", "data")
    }
    save_settings(settings)
    return redirect(url_for("index"))

@app.route("/api/regions", methods=["POST"])
def get_regions():
    settings = load_settings()
    dimension = request.form.get("dimension", "overworld")
    if dimension == "custom":
        dimension = request.form.get("custom_dimension", "").strip()

    base_world = settings.get("world_dir", "")
    if not base_world or not os.path.exists(base_world):
        return jsonify({"status": "error", "message": "Invalid world directory. Please check Global Settings."}), 400

    if dimension == "overworld":
        region_dir = os.path.join(base_world, "region")
    else:
        region_dir = os.path.join(base_world, dimension, "region")

    if not os.path.exists(region_dir):
        return jsonify({"status": "success", "regions": []})

    regions = []
    for f in os.listdir(region_dir):
        if f.endswith(".mca") and f.startswith("r."):
            parts = f.split(".")
            if len(parts) >= 3:
                try:
                    rx = int(parts[1])
                    rz = int(parts[2])
                    min_x = rx * 512
                    max_x = min_x + 511
                    min_z = rz * 512
                    max_z = min_z + 511
                    regions.append({
                        "name": f,
                        "label": f"X: {min_x} to {max_x} | Z: {min_z} to {max_z}",
                        "rx": rx,
                        "rz": rz
                    })
                except ValueError:
                    pass
    
    regions.sort(key=lambda item: (item["rx"], item["rz"]))
    return jsonify({"status": "success", "regions": regions})

@app.route("/start_prospecting", methods=["POST"])
def start_prospecting():
    settings = load_settings()
    dimension = request.form.get("dimension", "overworld")
    if dimension == "custom":
        dimension = request.form.get("custom_dimension", "").strip()
        if not dimension: return jsonify({"status": "error", "message": "Custom dimension required."}), 400
    
    regions_input = request.form.get("regions", "").strip()
    base_world = settings["world_dir"]
    if not base_world: return jsonify({"status": "error", "message": "World dir not set."}), 400
    
    region_dir = os.path.join(base_world, "region") if dimension == "overworld" else os.path.join(base_world, dimension, "region")
    if not os.path.exists(region_dir): return jsonify({"status": "error", "message": f"Region folder not found: {region_dir}"}), 400
    
    dimension_export_dir = os.path.join(settings["export_dir"], dimension)
    os.makedirs(dimension_export_dir, exist_ok=True)
    
    cmd = [
        sys.executable, "-u", "prospecting.py",
        "--region-dir", region_dir,
        "--dump-dir", settings["dump_dir"],
        "--export-dir", dimension_export_dir,
        "--cpu-usage", str(settings["cpu_usage"] / 100.0),
        "--skip-block-ids", settings["skip_block_ids"]
    ]
    if settings["only_ores"]: cmd.append("--only-ores")
    if settings["only_ores_and_fluids"]: cmd.append("--only-ores-and-fluids")
    if regions_input: cmd.extend(["--regions", regions_input])
        
    try:
        kwargs = {}
        if os.name == 'nt': kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **kwargs)
        RUNNING_PROCESSES[process.pid] = {"process": process, "cmd": f"Prospecting: {dimension}", "last_output": "Initializing..."}
        
        import threading
        def read_output(pid):
            proc = RUNNING_PROCESSES[pid]["process"]
            for line in iter(proc.stdout.readline, ''):
                if pid in RUNNING_PROCESSES and line.strip(): RUNNING_PROCESSES[pid]["last_output"] = line.strip()
            proc.stdout.close()
        threading.Thread(target=read_output, args=(process.pid,), daemon=True).start()
        
        return jsonify({"status": "success", "message": f"Started for '{dimension}'.", "pid": process.pid})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/ores/<dimension>")
def get_dimension_ores(dimension):
    dim_dir = os.path.join(load_settings()["export_dir"], dimension)
    dict_path = os.path.join(dim_dir, "dictionary.json")
    if not os.path.exists(dict_path):
        return jsonify([])
    try:
        with open(dict_path, "r", encoding="utf-8") as f:
            dictionary = json.load(f)
        unique_ores = set(dictionary.values())
        return jsonify(sorted(list(unique_ores)))
    except Exception:
        return jsonify([])

@app.route("/generate_export", methods=["POST"])
def generate_export():
    settings = load_settings()
    dimension = request.form.get("export_dim")
    export_type = request.form.get("export_type")
    ore_filter = request.form.get("ore_filter", "All Ores")
    
    if not dimension or not export_type: return jsonify({"status": "error", "message": "Missing dimension or type."}), 400
    dim_dir = os.path.join(settings["export_dir"], dimension)
    out_dir = os.path.join(settings["export_dir"], "_game_exports", dimension)
    if not os.path.exists(dim_dir): return jsonify({"status": "error", "message": "Prospected data not found."}), 400
        
    cmd = [sys.executable, "exporter.py", "--dim-dir", dim_dir, "--export-type", export_type, "--out-dir", out_dir]
    if ore_filter != "All Ores":
        cmd.extend(["--ore-filter", ore_filter])
        
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0: 
            msg = result.stdout.strip() if result.stdout else f"Successfully generated to {out_dir}"
            return jsonify({"status": "success", "message": msg})
        else: 
            return jsonify({"status": "error", "message": f"Export failed: {result.stderr or result.stdout}"}), 500
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stop_prospecting", methods=["POST"])
def stop_prospecting():
    pid = request.get_json().get("pid")
    if not pid: 
        return jsonify({"status": "error"}), 400
    try:
        RUNNING_PROCESSES[int(pid)]
    except KeyError as e: 
        return jsonify({"status": "error"}), 404
    try:
        proc = RUNNING_PROCESSES[int(pid)]["process"]
        if os.name == 'nt': subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
        else: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.terminate()
        del RUNNING_PROCESSES[int(pid)]
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error"}), 500

@app.route("/get_status", methods=["GET"])
def get_status():
    for pid in list(RUNNING_PROCESSES.keys()):
        if RUNNING_PROCESSES[pid]["process"].poll() is not None: del RUNNING_PROCESSES[pid]
    return jsonify({pid: {"name": info["cmd"], "output": info["last_output"]} for pid, info in RUNNING_PROCESSES.items()})

@app.route("/canvas")
def canvas(): return render_template("canvas.html")

@app.route("/api/dimensions")
def get_exported_dimensions():
    export_dir = load_settings()["export_dir"]
    if not os.path.exists(export_dir): return jsonify([])
    dim_names = {}
    for category, dims in DIMENSIONS.items():
        for dim_id, dim_name in dims.items():
            dim_names[dim_id] = dim_name
    return jsonify([{ "id": d, "name": dim_names.get(d, d) } for d in os.listdir(export_dir) if os.path.isdir(os.path.join(export_dir, d)) and not d.startswith("_")])

@app.route("/api/prospected_regions/<dimension>")
def get_prospected_regions(dimension):
    dim_dir = os.path.join(load_settings()["export_dir"], dimension)
    if not os.path.exists(dim_dir): return jsonify([])
    return jsonify([f[:-5] for f in os.listdir(dim_dir) if f.startswith("r.") and f.endswith(".json")])

@app.route("/api/data/<dimension>/<path:filename>")
def get_dimension_data(dimension, filename):
    return send_from_directory(os.path.join(load_settings()["export_dir"], dimension), filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
