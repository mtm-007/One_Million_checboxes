import fasthtml.common as fh
import monsterui.all as mui 

app, rt = fh.fast_app(hdrs=mui.Theme.blue.headers(), live=True)

def NavBar():
    return mui.NavBar(fh.A("Blog", href="/blog"),
                      fh.A("Team", href="/team"),
                      fh.A("Services", href="/services"),
                      brand=fh.H3("Agent Unlock"))

@rt
def index():
    return NavBar()

fh.serve()