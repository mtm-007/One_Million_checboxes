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
                    fh.Div( mui.H3(meta["title"]), 
                            fh.P(meta["description"]),
                            mui.DivFullySpaced(
                                fh.P(meta["author"],cls=mui.TextT.info),
                                fh.P(meta["date"], cls=mui.TextT.info)),
                            mui.DivFullySpaced(
                                mui.DivLAligned(*map(mui.Label, meta["categories"])),
                                fh.A("Read More", href=blog_post.to(fname=fname),
                                     cls=('uk-button', mui.ButtonT.primary)))
                            ,cls='space-y-2 w-full')
                    ))
@app.route("/")
def index():
    return fh.Titled("My Blog", mui.Grid(*map(BlogCard, os.listdir("posts")), cols=1))

@app.route
def blog_post(fname:str):
    with open(f"posts/{fname}", "r") as f: content = f.read()
    content = content.split("---")[2]
    return mui.Container(mui.render_md(content),cls=mui.ContainerT.sm)
    #return content

@app.route
def theme():
    from fasthtml.components import Uk_theme_switcher
    return Uk_theme_switcher()


fh.serve()