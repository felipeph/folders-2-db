import sqlite3
import json
import os
import argparse
import collections
from datetime import datetime

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Database Analysis Report</title>
    <!-- Tailwind CSS (via CDN for fast, modern styling) -->
    <script src="https://cdn.tailwindcss.com"></script>
    <!-- Chart.js -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <!-- DataTables CSS -->
    <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
    <!-- jQuery (required for DataTables) -->
    <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
    <!-- DataTables JS -->
    <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
    
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: 'Inter', sans-serif; }
        .card { background-color: #1e293b; border-radius: 0.75rem; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5); padding: 1.5rem; transition: transform 0.2s; }
        .card:hover { transform: translateY(-5px); }
        .chart-container { position: relative; height: 350px; width: 100%; }
        
        /* DataTables Dark Mode Customization */
        table.dataTable tbody tr { background-color: #1e293b; }
        table.dataTable tbody tr:nth-of-type(odd) { background-color: #273549; }
        table.dataTable tbody tr:hover { background-color: #334155; }
        table.dataTable, table.dataTable th, table.dataTable td { border-color: #334155; color: #cbd5e1; }
        .dataTables_wrapper .dataTables_length, .dataTables_wrapper .dataTables_filter, .dataTables_wrapper .dataTables_info, .dataTables_wrapper .dataTables_processing, .dataTables_wrapper .dataTables_paginate { color: #cbd5e1; }
        .dataTables_wrapper .dataTables_paginate .paginate_button { color: #cbd5e1 !important; }
        
        .tab-btn { padding: 0.75rem 1.5rem; cursor: pointer; border-bottom: 2px solid transparent; font-weight: 600; color: #94a3b8; }
        .tab-btn:hover { color: #f8fafc; }
        .tab-btn.active { color: #38bdf8; border-bottom-color: #38bdf8; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
    </style>
</head>
<body class="p-6">
    <div class="max-w-7xl mx-auto">
        <header class="mb-8 flex justify-between items-end border-b border-slate-700 pb-4">
            <div>
                <h1 class="text-4xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-sky-400 to-blue-600">Database Analysis</h1>
                <p class="text-slate-400 mt-2">File: <span class="font-mono text-slate-300" id="db-name"></span></p>
            </div>
            <div class="text-right">
                <p class="text-sm text-slate-400">Generated at</p>
                <p class="font-semibold text-slate-200" id="gen-date"></p>
            </div>
        </header>

        <!-- KPI Cards -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
            <div class="card border-t-4 border-sky-500">
                <h3 class="text-slate-400 text-sm uppercase font-semibold">Total Files</h3>
                <p class="text-3xl font-bold mt-2" id="kpi-files">0</p>
            </div>
            <div class="card border-t-4 border-emerald-500">
                <h3 class="text-slate-400 text-sm uppercase font-semibold">Total Size</h3>
                <p class="text-3xl font-bold mt-2" id="kpi-size">0 B</p>
            </div>
            <div class="card border-t-4 border-amber-500">
                <h3 class="text-slate-400 text-sm uppercase font-semibold">Exact Duplicates</h3>
                <p class="text-3xl font-bold mt-2" id="kpi-dups">0</p>
                <p class="text-xs text-slate-400 mt-1" id="kpi-dups-size">Wasted: 0 B</p>
            </div>
            <div class="card border-t-4 border-purple-500">
                <h3 class="text-slate-400 text-sm uppercase font-semibold">Extensions</h3>
                <p class="text-3xl font-bold mt-2" id="kpi-exts">0</p>
            </div>
        </div>

        <!-- Tabs -->
        <div class="flex space-x-2 border-b border-slate-700 mb-6 overflow-x-auto">
            <button class="tab-btn active whitespace-nowrap" onclick="switchTab('overview', this)">Overview</button>
            <button class="tab-btn whitespace-nowrap" onclick="switchTab('top-files', this)">Top 100 Files</button>
            <button class="tab-btn whitespace-nowrap" onclick="switchTab('duplicates', this)">Duplicates</button>
            <button class="tab-btn whitespace-nowrap" id="tab-btn-exif" onclick="switchTab('exif', this)" style="display:none;">EXIF Data</button>
        </div>

        <!-- Overview Tab -->
        <div id="overview" class="tab-content active">
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                <div class="card">
                    <h3 class="text-lg font-semibold mb-4 text-slate-200">File Extensions (by Size)</h3>
                    <div class="chart-container">
                        <canvas id="extChart"></canvas>
                    </div>
                </div>
                <div class="card">
                    <h3 class="text-lg font-semibold mb-4 text-slate-200">Timeline (Files Modified by Month)</h3>
                    <div class="chart-container">
                        <canvas id="timelineChart"></canvas>
                    </div>
                </div>
            </div>
        </div>

        <!-- Top Files Tab -->
        <div id="top-files" class="tab-content">
            <div class="card">
                <h3 class="text-lg font-semibold mb-4 text-slate-200">Top 100 Largest Files</h3>
                <div class="overflow-x-auto">
                    <table id="topFilesTable" class="display" style="width:100%">
                        <thead>
                            <tr>
                                <th>Filename</th>
                                <th>Filepath</th>
                                <th>Size</th>
                                <th>Modified Date</th>
                            </tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Duplicates Tab -->
        <div id="duplicates" class="tab-content">
            <div class="card">
                <h3 class="text-lg font-semibold mb-4 text-slate-200">Identical Files (Size + Partial Hash match)</h3>
                <div class="overflow-x-auto">
                    <table id="dupsTable" class="display" style="width:100%">
                        <thead>
                            <tr>
                                <th>Hash Group</th>
                                <th>Copies</th>
                                <th>Size per File</th>
                                <th>Wasted Space</th>
                                <th>Paths</th>
                            </tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- EXIF Tab -->
        <div id="exif" class="tab-content">
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div class="card">
                    <h3 class="text-lg font-semibold mb-4 text-slate-200">Top Camera Models</h3>
                    <div class="chart-container">
                        <canvas id="exifModelChart"></canvas>
                    </div>
                </div>
                <div class="card">
                    <h3 class="text-lg font-semibold mb-4 text-slate-200">Camera Details</h3>
                    <div class="overflow-x-auto">
                        <table id="exifTable" class="display" style="width:100%">
                            <thead>
                                <tr>
                                    <th>Make / Model</th>
                                    <th>Count</th>
                                </tr>
                            </thead>
                            <tbody></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const reportData = {REPORT_DATA};

        function formatBytes(bytes, decimals = 2) {
            if (!+bytes) return '0 Bytes';
            const k = 1024;
            const dm = decimals < 0 ? 0 : decimals;
            const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB', 'PB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
        }

        // Initialize UI
        document.getElementById('db-name').innerText = reportData.db_name;
        document.getElementById('gen-date').innerText = new Date(reportData.generated_at).toLocaleString();
        
        document.getElementById('kpi-files').innerText = reportData.overview.total_files.toLocaleString();
        document.getElementById('kpi-size').innerText = formatBytes(reportData.overview.total_size_bytes);
        document.getElementById('kpi-exts').innerText = Object.keys(reportData.extensions).length.toLocaleString();
        
        let totalDupWasted = 0;
        let totalDupCount = 0;
        reportData.duplicates.forEach(d => {
            totalDupCount += (d.count - 1);
            totalDupWasted += d.size_bytes * (d.count - 1);
        });
        document.getElementById('kpi-dups').innerText = totalDupCount.toLocaleString();
        document.getElementById('kpi-dups-size').innerText = `Wasted: ${formatBytes(totalDupWasted)}`;

        // Tab Switching
        window.switchTab = function(tabId, btn) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            btn.classList.add('active');
        };

        // Charts Configurations
        Chart.defaults.color = '#94a3b8';
        Chart.defaults.font.family = 'Inter';

        // Extension Chart
        const extData = Object.entries(reportData.extensions)
            .sort((a, b) => b[1].size - a[1].size)
            .slice(0, 15); // top 15
        
        const otherSize = Object.entries(reportData.extensions)
            .sort((a, b) => b[1].size - a[1].size)
            .slice(15)
            .reduce((sum, curr) => sum + curr[1].size, 0);
            
        if (otherSize > 0) extData.push(['Other', { size: otherSize }]);

        new Chart(document.getElementById('extChart'), {
            type: 'doughnut',
            data: {
                labels: extData.map(d => d[0] || 'Unknown'),
                datasets: [{
                    data: extData.map(d => d[1].size),
                    backgroundColor: [
                        '#38bdf8', '#818cf8', '#34d399', '#fbbf24', '#f87171', 
                        '#a78bfa', '#2dd4bf', '#fb923c', '#f472b6', '#a3e635',
                        '#60a5fa', '#c084fc', '#4ade80', '#facc15', '#f87171', '#94a3b8'
                    ],
                    borderWidth: 0,
                    hoverOffset: 10
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: 'right' },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return ` ${context.label}: ${formatBytes(context.raw)}`;
                            }
                        }
                    }
                }
            }
        });

        // Timeline Chart
        const timelineEntries = Object.entries(reportData.timeline).sort((a, b) => a[0].localeCompare(b[0]));
        new Chart(document.getElementById('timelineChart'), {
            type: 'bar',
            data: {
                labels: timelineEntries.map(d => d[0]),
                datasets: [{
                    label: 'Files Modified',
                    data: timelineEntries.map(d => d[1]),
                    backgroundColor: '#38bdf8',
                    borderRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: { beginAtZero: true, grid: { color: '#334155' } },
                    x: { grid: { display: false } }
                }
            }
        });

        // Initialize DataTables
        $(document).ready(function() {
            // Top Files Table
            $('#topFilesTable').DataTable({
                data: reportData.top_100_files,
                columns: [
                    { data: 'filename', render: $.fn.dataTable.render.text() },
                    { data: 'filepath', render: function(data) {
                        return `<div class="text-xs font-mono text-slate-400 break-all" style="max-width: 300px;">${data}</div>`;
                    }},
                    { data: 'size_bytes', render: function(data) {
                        return formatBytes(data);
                    }},
                    { data: 'mtime', render: function(data) {
                        return new Date(data * 1000).toLocaleString();
                    }}
                ],
                order: [[2, 'desc']],
                pageLength: 20
            });

            // Duplicates Table
            const dupsArray = reportData.duplicates.map(d => ({
                hash: d.partial_hash,
                count: d.count,
                size_per: d.size_bytes,
                wasted: d.size_bytes * (d.count - 1),
                paths: d.files.join('<br>')
            }));

            $('#dupsTable').DataTable({
                data: dupsArray,
                columns: [
                    { data: 'hash', render: function(data) {
                        return `<span class="font-mono text-xs">${data}</span>`;
                    }},
                    { data: 'count' },
                    { data: 'size_per', render: function(data) { return formatBytes(data); }},
                    { data: 'wasted', render: function(data) { 
                        return `<span class="text-amber-400 font-bold">${formatBytes(data)}</span>`; 
                    }},
                    { data: 'paths', render: function(data) {
                        return `<div class="text-xs font-mono text-slate-400 break-all" style="max-height: 100px; overflow-y: auto;">${data}</div>`;
                    }}
                ],
                order: [[3, 'desc']], // sort by wasted space
                pageLength: 10
            });

            // EXIF Chart & Table if data exists
            if (Object.keys(reportData.exif).length > 0) {
                document.getElementById('tab-btn-exif').style.display = 'block';
                
                const exifEntries = Object.entries(reportData.exif).sort((a, b) => b[1] - a[1]);
                
                $('#exifTable').DataTable({
                    data: exifEntries.map(e => ({ model: e[0], count: e[1] })),
                    columns: [
                        { data: 'model', render: $.fn.dataTable.render.text() },
                        { data: 'count' }
                    ],
                    order: [[1, 'desc']],
                    pageLength: 10
                });

                const topExif = exifEntries.slice(0, 10);
                new Chart(document.getElementById('exifModelChart'), {
                    type: 'bar',
                    data: {
                        labels: topExif.map(d => d[0]),
                        datasets: [{
                            label: 'Photos',
                            data: topExif.map(d => d[1]),
                            backgroundColor: '#a78bfa',
                            borderRadius: 4
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        indexAxis: 'y',
                        scales: {
                            x: { beginAtZero: true, grid: { color: '#334155' } },
                            y: { grid: { display: false } }
                        }
                    }
                });
            }
        });
    </script>
</body>
</html>
"""

def generate_report(db_path: str, output_dir: str = "reports"):
    if not os.path.exists(db_path):
        print(f"Error: Database {db_path} not found.")
        return

    os.makedirs(output_dir, exist_ok=True)
    db_name = os.path.basename(db_path)
    
    print(f"Analyzing {db_name}...")
    
    report_data = {
        "db_name": db_name,
        "generated_at": datetime.now().isoformat(),
        "overview": {
            "total_files": 0,
            "total_size_bytes": 0
        },
        "extensions": {},
        "timeline": collections.defaultdict(int),
        "top_100_files": [],
        "duplicates": [],
        "exif": collections.defaultdict(int)
    }

    duplicates_tracker = collections.defaultdict(list)

    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Top 100 Largest Files
    print("Fetching top 100 largest files...")
    cursor.execute("SELECT filename, filepath, size_bytes, mtime FROM files ORDER BY size_bytes DESC LIMIT 100")
    for row in cursor:
        report_data["top_100_files"].append({
            "filename": row[0],
            "filepath": row[1],
            "size_bytes": row[2],
            "mtime": row[3]
        })

    # Aggregate data
    print("Aggregating statistics...")
    cursor.execute("SELECT filename, filepath, size_bytes, mtime, partial_hash, exif_data FROM files")
    
    count = 0
    for row in cursor:
        count += 1
        if count % 10000 == 0:
            print(f"Processed {count} files...")
            
        filename = row[0]
        filepath = row[1]
        size_bytes = row[2] or 0
        mtime = row[3] or 0
        partial_hash = row[4]
        exif_json = row[5]

        # Overview
        report_data["overview"]["total_files"] += 1
        report_data["overview"]["total_size_bytes"] += size_bytes

        # Extension
        ext = os.path.splitext(filename)[1].lower()
        if not ext:
            ext = 'none'
        if ext not in report_data["extensions"]:
            report_data["extensions"][ext] = {"count": 0, "size": 0}
        report_data["extensions"][ext]["count"] += 1
        report_data["extensions"][ext]["size"] += size_bytes

        # Timeline
        try:
            dt = datetime.fromtimestamp(mtime)
            month_key = dt.strftime("%Y-%m")
            if dt.year > 1970 and dt.year <= datetime.now().year:
                report_data["timeline"][month_key] += 1
        except Exception:
            pass 

        # Duplicates Tracking
        if partial_hash and size_bytes > 0:
            dup_key = f"{partial_hash}_{size_bytes}"
            duplicates_tracker[dup_key].append(filepath)

        # EXIF
        if exif_json:
            try:
                exif_data = json.loads(exif_json)
                make = exif_data.get('Make', '').strip()
                model = exif_data.get('Model', '').strip()
                if make or model:
                    if make and model and not model.lower().startswith(make.lower()):
                        cam = f"{make} - {model}"
                    else:
                        cam = model if model else make
                    report_data["exif"][cam] += 1
            except Exception:
                pass

    conn.close()

    # Process Duplicates
    print("Identifying duplicates...")
    for key, paths in duplicates_tracker.items():
        if len(paths) > 1:
            parts = key.split('_')
            partial_hash = parts[0]
            size = int(parts[1])
            report_data["duplicates"].append({
                "partial_hash": partial_hash,
                "size_bytes": size,
                "count": len(paths),
                "files": paths
            })

    # Sort duplicates by wasted space (descending)
    report_data["duplicates"].sort(key=lambda x: x["size_bytes"] * (x["count"] - 1), reverse=True)
    report_data["duplicates"] = report_data["duplicates"][:1000]

    # Output Files
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(db_name)[0]
    
    json_path = os.path.join(output_dir, f"{base_name}_report_{timestamp}.json")
    html_path = os.path.join(output_dir, f"{base_name}_dashboard_{timestamp}.html")
    
    print("Generating files...")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=4, ensure_ascii=False)
        
    html_content = HTML_TEMPLATE.replace("{REPORT_DATA}", json.dumps(report_data))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Success! Report generated:")
    print(f"   HTML Dashboard: {os.path.abspath(html_path)}")
    print(f"   Raw Data JSON:  {os.path.abspath(json_path)}")
    return json_path, html_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate an interactive HTML dashboard from a SQLite database.")
    parser.add_argument("db_path", help="Path to the .sqlite database file.")
    parser.add_argument("--out", "-o", default="reports", help="Output directory for reports (default: reports).")
    args = parser.parse_args()
    
    generate_report(args.db_path, args.out)
