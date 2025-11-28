import modal

app = modal.App("example-fasthtml")

@app.function(
    image=modal.Image.debian_slim(python_version="3.12").pip_install(
        "python-fasthtml==0.12.35"
    ),
)
@modal.asgi_app()
def serve():
    import json
    import fasthtml.common as fh
    
    app, rt = fh.fast_app(
        hdrs=(fh.Script(src="https://cdn.plot.ly/plotly-2.32.0.min.js"),),
        debug=True)

    data = json.dumps({
        "data": [{"x": [1,2,3,4], "type": "scatter"},
                 {"x": [1,2,3,4],"y": [16,5,11,9], "type":"scatter"}],
        "title": "plotly chart in FastHTML ",
        "description": "This is a demo dashboard",
        "type": "scatter"
    })

    @rt("/chart")
    def get():
        return fh.Titled(
            "Chart Demo", 
            fh.Div(id="myDiv"),
            fh.Script(f"var data = {data}; Plotly.newPlot('myDiv', data);"))
    @rt("/opps")
    def get():
        1/0
        return fh.Titled("FastHtml Error!", fh.P("Let's error!"))

    @app.get('/')
    def home():
        return fh.Div(fh.P("Modal deployment first try!"), hx_get="/change")

    return app