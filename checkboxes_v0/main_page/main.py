import fasthtml.common as fh
import monsterui.all as mui 

app, rt = fh.fast_app(hdrs=mui.Theme.blue.headers())#, live=True)

def NavBar():
    return mui.NavBar(fh.A("Blog", href="/blog"),
                      fh.A("Team", href=team),
                      fh.A("Services", href="/services"),
                      fh.A("Theme", href=theme),
                      brand=fh.H3("Agent Unlock Labs"))

def TeamCard(name, role):#, location="Remote"):
        return fh.Card(
            mui.DivLAligned(
                mui.DiceBearAvatar(name, h=24, w=24),
                fh.Div(fh.H3(name), fh.P(role))),
            footer=mui.DivFullySpaced(
                #mui.DivHStacked(mui.UkIcon("map-pin", height=16), fh.P(location)),
                mui.DivRAligned(*(mui.UkIconLink(icon, height=16) for icon in ("mail", "linkedin", "github", "rss", "globe")))) )
 

#     return fh.Grid(*team, cols_sm=1, cols_md=1, cols_lg=2, cols_xl=3)


@rt("/")
def team():
    return  NavBar(), fh.Titled("Team", fh.P("Agent Unlock Team"), 
            fh.Grid(TeamCard("Agent Name", "Founder"),#, "Remote, US"),
                    TeamCard("Agent Name", "Founder"),#, "Remote, US"),
                    TeamCard("Agent Name", "Founder")#, "Remote, US"),
                             ))

@rt("/theme")
def theme():
    return NavBar(), mui.ThemePicker()

fh.serve()