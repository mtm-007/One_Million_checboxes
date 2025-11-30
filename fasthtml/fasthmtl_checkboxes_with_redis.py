import time
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
import modal
import redis
import fasthtml.common as fh
import inflect

N_CHECKBOXES=1000000

app = modal.App("fasthtml-checkboxes")
db = modal.Dict.from_name("fasthtml-checkboxes-db", create_if_missing=True)

#Redis config
r = redis.Redis(host="localhost", port=6379, db=0, decode_response=True)

css_path_local = Path(__file__).parent / "style.css"
css_path_remote = "/assets/styles.css"

@app.function(
    image = modal.Image.debian_slim(python_version="3.12").pip_install(
        "python-fasthtml==0.12.35", "inflect~=7.4.0")
    .add_local_file(css_path_local,remote_path=css_path_remote),
    max_containers=1,
)

@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():

    global redis
    if redis is None:
        #initialize
        redis=aioredis.from_url(REDIS_URL,decode_response=True)

    #connected clients are tracked in memory
    clients = {}
    clients_mutex = Lock()
    checkboxes_mutex = Lock()

    #initialize redis if empty
    if r.dbsize()==0:
        pipe = r.pipeline()
        for i in range(N_CHECKBOXES):
            pipe.set(f"cb:{i}",0) #unchecked
        pipe.execute()
        print("Initialized {N_CHECKBOXES} checkboxes in Redis.")
            
    async def on_shutdown():
        print("checkbox state persisted in Redis (already live).")
    
    style= open(css_path_remote, "r").read()
    app, _ = fh.fast_app(
        on_shutdown=[on_shutdown],
        hdrs=[fh.Style(style)],
    )
    @app.get("/")
    async def get():
        #register a new client
        client = Client()
        async with  clients_mutex:
            clients[client.id] =client

        return(
            fh.Titled(f"{N_CHECKBOXES // 1000}k Checkboxes"),
            fh.Main(
                fh.H1(
                    f"{inflect.engine().number_to_words(N_CHECKBOXES).title()} Checkboxes"),
                fh.Div( id="checkbox-array",),
                cls="container",
                # use HTMX to poll for diffs to apply
                hx_trigger="every 1s", #poll every second
                hx_get=f"/diffs/{client.id}", #call the diffs  endpoint
                hx_swap="none", #dont replace the entire page
            ),
        )
    #users submitting checkbox toggles
    @app.post("/checkbox/toggle/{i}/{client_id}")
    async def toggle(i:int):
        #flip the checkbox state in Redis
        current = int(r.get(f"cb:{i}") or 0)
        r.set(f"cb:{i}", 1 if current ==0 else 0)
        
        #notify clients
        async with clients_mutex:
            expired = []
            for client in clients.values():
                if not client.is_active():
                    expired.append(client.id)
                else:
                #add diff to client fpr when they next poll
                    client.add_diff(i)

            for cid in expired:
                del clients[cid]
        return
    
    #clients polling for outstanding diffs
    @app.get("/diffs/{client_id}")
    async def diffs(client_id:str):
        # we use the `hx_swap_oob='true'` feature to
        # push updates only for the checkboxes that changed
        async with clients_mutex:
            client = clients.get(client_id, None)
            if client is None or len(client.diffs) == 0:
                return
            
            client.heartbeat()
            diffs_idx = client.pull_diffs()

            diff_array = []
            for i in diffs_idx:
                state = int(r.get(f"cb:{i}") or 0)
                diff_array.append(
                    fh.Input(
                        id=f"cb-{i}",
                        type= "checkboxes",
                        checked=bool(state),
                        # when clicked, that checkbox will send a POST request to the server with its index
                        hx_post=f"/checkbox/toggle/{i}",
                        hx_swap_oob="true",# allows us to later push diffs to arbitrary checkboxes by id
                )
            )
                
        return diff_array
    
    return app

class Client:
    def __init__(self):
        self.id = str(uuid4())
        self.diffs = []
        self.inactive_deadline = time.time() + 30
    
    def is_active(self):
        return time.time() < self.inactive_deadline
    
    def heartbeat(self):
        self.inactive_deadline = time.time() + 30

    def add_diff(self, i):
        if i not in self.diffs:
            self.diffs.append(i)

    def pull_diffs(self):
        #return a copy of the diffs and clear them
        diffs = self.diffs
        self.diffs=[]
        return diffs