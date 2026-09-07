import json
import os
from datetime import datetime

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Relatório de Duplicatas</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f5f5f5; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        h1 { color: #2c3e50; }
        .stats { display: flex; gap: 20px; margin-bottom: 20px; }
        .stat-box { background: #3498db; color: white; padding: 15px; border-radius: 5px; flex: 1; text-align: center; }
        .stat-box h3 { margin: 0; font-size: 24px; }
        .stat-box p { margin: 5px 0 0; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background-color: #f8f9fa; cursor: pointer; }
        th:hover { background-color: #e9ecef; }
        tr:hover { background-color: #f5f5f5; }
        .path { word-break: break-all; font-family: monospace; font-size: 12px; }
        input[type="text"] { width: 100%; padding: 10px; margin-bottom: 20px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .tag { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; }
        .tag.dup { background: #ffeaa7; color: #d35400; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Relatório de Análise de Duplicatas</h1>
        
        <div class="stats">
            <div class="stat-box">
                <h3 id="stat-total">0</h3>
                <p>Total de Arquivos Analisados</p>
            </div>
            <div class="stat-box">
                <h3 id="stat-dups">0</h3>
                <p>Arquivos Duplicados Encontrados</p>
            </div>
            <div class="stat-box">
                <h3 id="stat-space">0 MB</h3>
                <p>Espaço Desperdiçado (Estimativa)</p>
            </div>
        </div>

        <input type="text" id="searchInput" placeholder="Filtrar por nome de arquivo ou caminho..." onkeyup="filterTable()">

        <table id="dupTable">
            <thead>
                <tr>
                    <th onclick="sortTable(0)">Arquivo Novo (Pasta Local)</th>
                    <th onclick="sortTable(1)">Tamanho</th>
                    <th onclick="sortTable(2)">Duplicata Localizada no BD (HDD)</th>
                </tr>
            </thead>
            <tbody id="tableBody">
                <!-- Data will be injected here -->
            </tbody>
        </table>
    </div>

    <script>
        const reportData = {REPORT_DATA};
        
        // Populate stats
        document.getElementById('stat-total').innerText = reportData.total_analyzed;
        document.getElementById('stat-dups').innerText = reportData.duplicates.length;
        
        let wastedBytes = 0;
        reportData.duplicates.forEach(d => wastedBytes += d.size_bytes);
        document.getElementById('stat-space').innerText = (wastedBytes / (1024 * 1024)).toFixed(2) + ' MB';

        // Populate table
        const tbody = document.getElementById('tableBody');
        reportData.duplicates.forEach(dup => {
            const tr = document.createElement('tr');
            
            // Format size
            const sizeKB = (dup.size_bytes / 1024).toFixed(1) + ' KB';
            
            tr.innerHTML = `
                <td><div class="path">${dup.scanned_file}</div></td>
                <td>${sizeKB}</td>
                <td><div class="path"><span class="tag dup">Encontrado!</span><br>${dup.db_file}</div></td>
            `;
            tbody.appendChild(tr);
        });

        function filterTable() {
            let input = document.getElementById("searchInput");
            let filter = input.value.toUpperCase();
            let table = document.getElementById("dupTable");
            let tr = table.getElementsByTagName("tr");

            for (let i = 1; i < tr.length; i++) {
                let td0 = tr[i].getElementsByTagName("td")[0];
                let td2 = tr[i].getElementsByTagName("td")[2];
                if (td0 || td2) {
                    let txtValue0 = td0.textContent || td0.innerText;
                    let txtValue2 = td2.textContent || td2.innerText;
                    if (txtValue0.toUpperCase().indexOf(filter) > -1 || txtValue2.toUpperCase().indexOf(filter) > -1) {
                        tr[i].style.display = "";
                    } else {
                        tr[i].style.display = "none";
                    }
                }       
            }
        }
        
        function sortTable(n) {
            let table, rows, switching, i, x, y, shouldSwitch, dir, switchcount = 0;
            table = document.getElementById("dupTable");
            switching = true;
            dir = "asc"; 
            while (switching) {
                switching = false;
                rows = table.rows;
                for (i = 1; i < (rows.length - 1); i++) {
                    shouldSwitch = false;
                    x = rows[i].getElementsByTagName("TD")[n];
                    y = rows[i + 1].getElementsByTagName("TD")[n];
                    if (dir == "asc") {
                        if (x.innerHTML.toLowerCase() > y.innerHTML.toLowerCase()) {
                            shouldSwitch = true;
                            break;
                        }
                    } else if (dir == "desc") {
                        if (x.innerHTML.toLowerCase() < y.innerHTML.toLowerCase()) {
                            shouldSwitch = true;
                            break;
                        }
                    }
                }
                if (shouldSwitch) {
                    rows[i].parentNode.insertBefore(rows[i + 1], rows[i]);
                    switching = true;
                    switchcount ++;      
                } else {
                    if (switchcount == 0 && dir == "asc") {
                        dir = "desc";
                        switching = true;
                    }
                }
            }
        }
    </script>
</body>
</html>
"""

def generate_reports(project_name: str, total_analyzed: int, duplicates: list, output_dir: str = "reports"):
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(output_dir, f"{project_name}_report_{timestamp}.json")
    html_path = os.path.join(output_dir, f"{project_name}_report_{timestamp}.html")
    
    data = {
        "project": project_name,
        "generated_at": datetime.now().isoformat(),
        "total_analyzed": total_analyzed,
        "duplicates": duplicates
    }
    
    # Generate JSON
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        
    # Generate HTML
    html_content = HTML_TEMPLATE.replace("{REPORT_DATA}", json.dumps(data))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    return json_path, html_path
