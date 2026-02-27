import fasthtml.common as fh
import monsterui.all as mui
import os,yaml

app = fh.FastHTML(hdrs=mui.Theme.blue.headers(), live=True)

def BlogCard(fname):
    with open(f"posts/{fname}", "r") as f:content = f.read()
    meta = content.split("---")[1]
    meta = yaml.safe_load(meta)
        
    return mui.Card(mui.DivHStacked(
                    fh.Img(src=meta["image"]),
                    fh.Div(
                        fh.H3(meta["title"]), 
                        fh.P(meta["description"]),
                        fh.P(meta["author"]),
                        fh.P(meta["date"]),
                        fh.P(meta["categories"]))
                    ))
@app.route("/")
def index():
    return map(BlogCard, os.listdir("posts"))

fh.serve()