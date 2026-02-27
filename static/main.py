import fasthtml.common as fh
import monsterui.all as mui
import os 

app = fh.FastHTML(hdrs=mui.Theme.blue.headers(), live=True)

@app.route("/")
def index():
    return map(mui.Card, os.listdir("posts"))

fh.serve()