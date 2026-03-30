import fasthtml.common as fh
import monsterui.all as mui 

app, rt = fh.fast_app(hdrs=mui.Theme.blue.headers(), live=True)

@rt
def index():
    return "Hello World!"

fh.serve()