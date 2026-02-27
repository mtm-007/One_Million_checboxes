import fasthtml.common as fh
import monsterui.all as mui 

#app, rt = fh.fast_app(hdrs=Theme.slate.headers())
app = fh.FastHTML(hdrs=mui.Theme.blue.headers())

@app.route("/")
def index():
    return fh.P(
        "Hello blog page!"
    )
fh.serve()