from fasthtml.common import *
#from fasthtml import common as fh


app, rt, todos, Todo = fast_app(
        'todos.db',live=True, 
        tbls={"todos":dict(id=int, title=str, done=bool, pk='id')})

@rt('/')
def get():
    todos.insert(Todo(title="first todo", done=False))
    items = [Li(o) for o in todos()]
    return Titled('Todos List',
                  Ul(*items),
                  )

serve()
