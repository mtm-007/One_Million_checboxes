from fasthtml.common import *
#from fasthtml import common as fh


app, rt = fast_app(live=True)

@rt('/')
def get():return Div(P("FIrst FastHtml webpage with python !"))

serve()
