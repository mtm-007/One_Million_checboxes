import modal
import json
from fasthtml.common import FastHTML, Script, Div, P, Titled

app = modal.App("FasthtmlWebPage")

image=modal.Image.debian_slim(python_version="3.12").pip_install("python-fasthtml==0.12.35")

ui = FastHTML(
        hdrs=(Script(src="https://cdn.plot.ly/plotly-2.32.0.min.js")),
        debug=True)

data = json.dumps({
        "data": [{"x": [1,2,3,4], "type": "scatter"},
                 {"x": [1,2,3,4],"y": [16,5,11,9], "type":"scatter"}],
        "title": "plotly chart in FastHTML ",
        "description": "This is a demo dashboard",
        "type": "scatter"
    })

@ui.get("/chart")
def get():
    return Titled(
        "Chart Demo", 
        Div(id="myDiv"),
        Script(f"var data = {data}; Plotly.newPlot('myDiv', data);"))

@ui.get("/opps")
def get():
    1/0
    return Titled("FastHtml Error!", P("Let's error!"))

@ui.get('/home')
def get():
    return Div(P("Modal deployment first try!"), hx_get="/change")

@ui.get("/{name}/{age}")
def get(name:str, age:int):
    return Titled(f"Hello {name.title()}, age {age}")

@ui.get("/")
def get():
    return Titled("HTTP GET", P("Handle GET"))

@ui.post("/")
def post():
    return Titled("HTTP POST", P("Handle POST"))

@app.function(image=image,min_containers=1)
@modal.asgi_app()
def serve():
    return ui