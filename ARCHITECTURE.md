<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GitHub Architecture Preview</title>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            line-height: 1.6;
        }
        
        .github-container {
            max-width: 1280px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        
        /* GitHub-style header */
        .github-header {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 16px;
            margin-bottom: 24px;
        }
        
        .github-header h1 {
            font-size: 32px;
            font-weight: 600;
            color: #c9d1d9;
            margin-bottom: 8px;
        }
        
        /* GitHub-style content blocks */
        .content-section {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 24px;
            margin-bottom: 24px;
        }
        
        .content-section h2 {
            font-size: 24px;
            font-weight: 600;
            color: #c9d1d9;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid #21262d;
        }
        
        .content-section h3 {
            font-size: 20px;
            font-weight: 600;
            color: #c9d1d9;
            margin-top: 24px;
            margin-bottom: 16px;
        }
        
        /* Mermaid diagram container */
        .mermaid-container {
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 16px;
            margin: 16px 0;
            overflow-x: auto;
        }
        
        .mermaid {
            display: flex;
            justify-content: center;
            min-height: 200px;
        }
        
        /* Code blocks */
        .code-block {
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 16px;
            margin: 16px 0;
            font-family: 'Courier New', monospace;
            font-size: 14px;
            overflow-x: auto;
        }
        
        .code-block pre {
            margin: 0;
            color: #c9d1d9;
        }
        
        /* GitHub breadcrumb */
        .breadcrumb {
            padding: 8px 0;
            margin-bottom: 16px;
            color: #8b949e;
            font-size: 14px;
        }
        
        .breadcrumb a {
            color: #58a6ff;
            text-decoration: none;
        }
        
        .breadcrumb a:hover {
            text-decoration: underline;
        }
        
        /* File info bar */
        .file-info {
            background: #161b22;
            border: 1px solid #30363d;
            border-bottom: none;
            border-radius: 6px 6px 0 0;
            padding: 8px 16px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
        }
        
        .file-icon {
            color: #8b949e;
        }
        
        /* Scroll hint */
        .scroll-hint {
            text-align: center;
            color: #8b949e;
            font-size: 12px;
            margin-top: 8px;
            font-style: italic;
        }
        
        /* Loading indicator */
        .loading {
            text-align: center;
            padding: 40px;
            color: #8b949e;
        }
        
        @media (max-width: 768px) {
            .github-container {
                padding: 20px 10px;
            }
            
            .content-section {
                padding: 16px;
            }
            
            .github-header h1 {
                font-size: 24px;
            }
        }
    </style>
</head>
<body>
    <div class="github-container">
        <!-- GitHub-style breadcrumb -->
        <div class="breadcrumb">
            <a href="#">your-repo</a> / <strong>ARCHITECTURE.md</strong>
        </div>
        
        <!-- File info bar -->
        <div class="file-info">
            <span class="file-icon">📄</span>
            <span>ARCHITECTURE.md</span>
        </div>
        
        <!-- Main content -->
        <div class="github-header">
            <h1>One Million Checkboxes - System Architecture</h1>
        </div>
        
        <div class="content-section">
            <h2>High-Level System Design</h2>
            <div class="mermaid-container">
                <div class="mermaid">
graph TD
    subgraph Client_Layer["👥 CLIENT LAYER"]
        B1[Browser 1<br/>HTMX]
        B2[Browser 2<br/>HTMX]
        BN[Browser N<br/>HTMX]
    end
    
    subgraph App_Layer["🖥️ APPLICATION LAYER"]
        FastHTML[FastHTML<br/>Web Server]
        
        subgraph Components["Core Components"]
            Routes[Routes /<br/>Handlers]
            ClientMgr[Client<br/>Manager]
            GeoAPI[Geo API<br/>Layer]
        end
    end
    
    subgraph Data_Layer["💾 DATA LAYER - Redis"]
        Bitmap[Bitmap<br/>1M checkboxes<br/>125KB]
        Visitor[Visitor Data<br/>Hash/Sorted Set]
        GeoCache[Geolocation<br/>Cache]
    end
    
    subgraph Ext_Layer["🌐 EXTERNAL APIS"]
        API1[ipwho.is]
        API2[ipapi.co]
        API3[ip-api.com]
    end
    
    subgraph Storage_Layer["💿 STORAGE LAYER"]
        Disk[Modal Volume<br/>/data/dump.rdb]
    end
    
    B1 -->|HTTP/HTMX| FastHTML
    B2 -->|HTTP/HTMX| FastHTML
    BN -->|HTTP/HTMX| FastHTML
    
    FastHTML --- Routes
    FastHTML --- ClientMgr
    FastHTML --- GeoAPI
    
    Routes -->|GETBIT/SETBIT| Bitmap
    Routes -->|BITCOUNT| Bitmap
    ClientMgr -->|Diff Queue| Visitor
    GeoAPI -->|GET/SET| GeoCache
    
    GeoAPI -->|Fallback 1| API1
    GeoAPI -->|Fallback 2| API2
    GeoAPI -->|Fallback 3| API3
    
    Bitmap -->|Persist| Disk
    Visitor -->|Persist| Disk
    GeoCache -->|Persist| Disk
    
    classDef clientStyle fill:#667eea,stroke:#764ba2,stroke-width:2px,color:#fff
    classDef appStyle fill:#48bb78,stroke:#38a169,stroke-width:2px,color:#fff
    classDef dataStyle fill:#ed8936,stroke:#dd6b20,stroke-width:2px,color:#fff
    classDef extStyle fill:#4299e1,stroke:#3182ce,stroke-width:2px,color:#fff
    classDef storageStyle fill:#9f7aea,stroke:#805ad5,stroke-width:2px,color:#fff
    
    class B1,B2,BN clientStyle
    class FastHTML,Routes,ClientMgr,GeoAPI appStyle
    class Bitmap,Visitor,GeoCache dataStyle
    class API1,API2,API3 extStyle
    class Disk storageStyle
                </div>
            </div>
            <div class="scroll-hint">← Scroll horizontally if needed →</div>
        </div>
        
        <div class="content-section">
            <h2>Data Flow: Checkbox Toggle</h2>
            <div class="mermaid-container">
                <div class="mermaid">
sequenceDiagram
    participant User
    participant Browser
    participant FastHTML
    participant Redis
    participant OtherClients
    
    User->>Browser: Click Checkbox #42
    Browser->>FastHTML: POST /toggle/42/{client_id}
    FastHTML->>Redis: GETBIT checkboxes_bitmap 42
    Redis-->>FastHTML: current_value = 0
    FastHTML->>Redis: SETBIT checkboxes_bitmap 42 1
    FastHTML->>FastHTML: Update local cache
    FastHTML->>OtherClients: Add #42 to diff queues
    FastHTML->>Redis: BITCOUNT (get stats)
    Redis-->>FastHTML: checked_count
    FastHTML-->>Browser: Return updated stats
    Browser->>User: Update UI
    
    Note over OtherClients: Poll every 500ms
    OtherClients->>FastHTML: GET /diffs/{client_id}
    FastHTML-->>OtherClients: Return checkbox #42 update
    OtherClients->>OtherClients: Update checkbox #42
                </div>
            </div>
        </div>
        
        <div class="content-section">
            <h2>Data Flow: Visitor Tracking</h2>
            <div class="mermaid-container">
                <div class="mermaid">
flowchart TD
    Start([New Visitor]) --> GetIP[Extract IP Address<br/>CF-Connecting-IP]
    GetIP --> CheckCache{Check Redis<br/>geo:ip}
    
    CheckCache -->|Cache Hit| UseCache[Use Cached Data]
    CheckCache -->|Cache Miss| API1[Try ipwho.is]
    
    API1 -->|Success| SaveCache[Save to Redis Cache]
    API1 -->|Fail| API2[Try ipapi.co]
    
    API2 -->|Success| SaveCache
    API2 -->|Fail| API3[Try ip-api.com]
    
    API3 -->|Success| SaveCache
    API3 -->|Fail| Fallback[Use Fallback Data]
    
    UseCache --> Record[Record Visitor]
    SaveCache --> Record
    Fallback --> Record
    
    Record --> CheckNew{New<br/>Visitor?}
    CheckNew -->|Yes| IncrCount[Increment<br/>total_visitors_count]
    CheckNew -->|No| UpdateVisit[Increment<br/>visit_count]
    
    IncrCount --> SaveRedis[Save to Redis<br/>visitor:ip]
    UpdateVisit --> SaveRedis
    
    SaveRedis --> AddSorted[Add to Sorted Set<br/>by timestamp]
    AddSorted --> End([Done])
    
    style Start fill:#667eea,color:#fff
    style End fill:#48bb78,color:#fff
    style SaveCache fill:#4299e1,color:#fff
    style Record fill:#ed8936,color:#fff
                </div>
            </div>
        </div>
        
        <div class="content-section">
            <h2>Technology Stack</h2>
            <div class="mermaid-container">
                <div class="mermaid">
mindmap
  root((One Million<br/>Checkboxes))
    Frontend
      HTMX
        Polling 500ms
        Lazy Loading
        OOB Swaps
      CSS
        Responsive Design
        Mobile First
    Backend
      FastHTML
        Python 3.12
        Async/Await
        ASGI
      Redis
        Bitmap 125KB
        Pub/Sub Ready
        Persistence
    Infrastructure
      Modal
        Serverless
        Auto-scaling
        Volumes
      Monitoring
        Latency Logs
        Throughput Metrics
    External
      Geolocation APIs
        ipwho.is
        ipapi.co
        ip-api.com
                </div>
            </div>
        </div>
        
        <div class="content-section">
            <h2>Performance Metrics</h2>
            <h3>Memory Usage Comparison</h3>
            <div class="mermaid-container">
                <div class="mermaid">
pie title Memory Usage Comparison
    "Bitmap (125 KB)" : 125
    "JSON List (8 MB)" : 8000
                </div>
            </div>
            
            <h3>Request Processing Timeline</h3>
            <div class="mermaid-container">
                <div class="mermaid">
gantt
    title Request Processing Timeline
    dateFormat X
    axisFormat %L ms
    
    section Checkbox Toggle
    Receive Request    :0, 5
    Get Current State  :5, 25
    Update Redis       :25, 50
    Update Cache       :50, 55
    Notify Clients     :55, 65
    Return Response    :65, 100
    
    section Visitor Tracking
    Extract IP         :0, 5
    Check Cache        :5, 15
    Geo API Call       :crit, 15, 515
    Save to Redis      :515, 540
    Record Visitor     :540, 565
                </div>
            </div>
        </div>
        
        <div class="content-section">
            <h2>How to Use in GitHub</h2>
            <ol style="margin-left: 24px; color: #c9d1d9;">
                <li style="margin: 8px 0;"><strong>Create a new file</strong> in your repo: <code style="background: #0d1117; padding: 2px 6px; border-radius: 3px;">ARCHITECTURE.md</code> or add to <code style="background: #0d1117; padding: 2px 6px; border-radius: 3px;">README.md</code></li>
                <li style="margin: 8px 0;"><strong>Paste the Mermaid code</strong> between triple backticks with <code style="background: #0d1117; padding: 2px 6px; border-radius: 3px;">mermaid</code> language tag</li>
                <li style="margin: 8px 0;"><strong>GitHub will automatically render</strong> the diagrams when you view the Markdown file</li>
            </ol>
            
            <div class="code-block">
                <pre>your-repo/
├── README.md
├── ARCHITECTURE.md  ← Add diagrams here
└── docs/
    └── system-design.md</pre>
            </div>
        </div>
    </div>
    
    <script>
        mermaid.initialize({ 
            startOnLoad: true,
            theme: 'dark',
            themeVariables: {
                primaryColor: '#667eea',
                primaryTextColor: '#fff',
                primaryBorderColor: '#764ba2',
                lineColor: '#8b949e',
                secondaryColor: '#48bb78',
                tertiaryColor: '#ed8936'
            }
        });
    </script>
</body>
</html>