## One Million Checkboxes

One Million checkboxes web app created with 
- FastHtml, 
- deployed with modal serverless and 
- implemented CI/CD with Github Actions.


## Live Deployed One Million Checkboxes
- Link(https://mtm-007--fasthtml-checkboxes-web.modal.run/)

## Design Architecture
┌──────────────────────────────────────────────────────────────────┐
│                          CLIENT LAYER                            │
│   ┌─────────────┐      ┌─────────────┐      ┌─────────────┐      │
│   │  Browser 1  │      │  Browser 2  │      │  Browser N  │      │
│   │   (HTMX)    │      │   (HTMX)    │      │   (HTMX)    │      │
│   └──────┬──────┘      └──────┬──────┘      └──────┬──────┘      │
│          │                    │                    │             │
│          └────────────────────┼────────────────────┘             │
│                               │                                  │
└───────────────────────────────┼──────────────────────────────────┘
                                │ HTTP/HTMX
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                        APPLICATION LAYER                         │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                   FastHTML Web Server                      │  │
│  │   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │  │
│  │   │    Routes    │  │    Client    │  │   Geo API    │     │  │
│  │   │   Handlers   │  │   Manager    │  │    Layer     │     │  │
│  │   └──────────────┘  └──────────────┘  └──────┬───────┘     │  │
│  └────────────────────────────┬─────────────────┼─────────────┘  │
└───────────────────────────────┼─────────────────┼────────────────┘
                                │                 │
               ┌────────────────┘                 │
               ▼                                  ▼
┌──────────────────────────────┐    ┌──────────────────────────────┐
│     DATA LAYER (Redis)       │    │        EXTERNAL APIS         │
│ ┌──────────────────────────┐ │    │    ┌────────────────────┐    │
│ │  Bitmap (1M checkboxes)  │ │    │    │      ipwho.is      │    │
│ │  Key: checkboxes_bitmap  │ │    │    │      ipapi.co      │    │
│ └──────────────────────────┘ │    │    │      ip-api.com    │    │
│ ┌──────────────────────────┐ │    │    └────────────────────┘    │
│ │  Visitors Data           │ │    └──────────────────────────────┘
│ │  Hash: visitor:{ip}      │ │
│ │  Sorted Set: visitors_log│ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │  Geolocation Cache       │ │
│ │  Key: geo:{ip}           │ │
│ └──────────────────────────┘ │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│        STORAGE LAYER         │
│ ┌──────────────────────────┐ │
│ │       Modal Volume       │ │
│ │    (Persistent Disk)     │ │
│ │     /data/dump.rdb       │ │
│ └──────────────────────────┘ │
└──────────────────────────────┘


# One Million Checkboxes - System Architecture

## High-Level System Design
```mermaid
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
    
    %% Client to Application
    B1 -->|HTTP/HTMX| FastHTML
    B2 -->|HTTP/HTMX| FastHTML
    BN -->|HTTP/HTMX| FastHTML
    
    %% Application to Components
    FastHTML --- Routes
    FastHTML --- ClientMgr
    FastHTML --- GeoAPI
    
    %% Components to Data Layer
    Routes -->|GETBIT/SETBIT| Bitmap
    Routes -->|BITCOUNT| Bitmap
    ClientMgr -->|Diff Queue| Visitor
    GeoAPI -->|GET/SET| GeoCache
    
    %% Geo API to External
    GeoAPI -->|Fallback 1| API1
    GeoAPI -->|Fallback 2| API2
    GeoAPI -->|Fallback 3| API3
    
    %% Data to Storage
    Bitmap -->|Persist| Disk
    Visitor -->|Persist| Disk
    GeoCache -->|Persist| Disk
    
    %% Styling
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
```

## Data Flow: Checkbox Toggle
```mermaid
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
```

## Data Flow: Visitor Tracking
```mermaid
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
```

## Technology Stack
```mermaid
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
```

## Performance Metrics
```mermaid
pie title Memory Usage Comparison
    "Bitmap (125 KB)" : 125
    "JSON List (8 MB)" : 8000
```
```mermaid
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
```
```

## How to Use in GitHub:

1. **Create a new file** in your repo: `ARCHITECTURE.md` or add to `README.md`

2. **Paste the Mermaid code** between triple backticks with `mermaid` language tag

3. **GitHub will automatically render** the diagrams when you view the Markdown file

4. **Example structure:**
```
   your-repo/
   ├── README.md
   ├── ARCHITECTURE.md  ← Add diagrams here
   └── docs/
       └── system-design.md