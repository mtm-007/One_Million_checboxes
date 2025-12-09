#import fasthtml.common as fh
#import monsterui.all as mui

from fasthtml.common import *
from monsterui.all import *


app, rt = fast_app(hdrs=Theme.blue.headers()
)

@rt
def index():
    socials = (('github', 'https://github.com/mtm-007'),
               ('twitter', 'https://x.com/Merhawi_MM'),
               ('linkedin', 'https://www.linkedin.com/in/merhawi-m-78a206163/'))
    return Titled("My info card",
                  Card(
                      H1("Welcome!"),
                      P("My information card app", cls=TextPresets.muted_sm),
                      P('Excited!'),
                      footer=DivLAligned(*[UkIconLink(icon, href=url) for icon, url in socials])
                  ))
serve()