import modal
import fasthtml.common as fh
from pathlib import Path


app = modal.App("fasthtml-todo-list")

Path('data').mkdir(exist_ok=True)

def render(todo):
    tid = f'todo-{todo.id}'
    toggle = fh.A('Toggle', hx_get=f'/toggle/{todo.id}', target_id=tid)
    delete = fh.A('Delete', hx_delete=f'/{todo.id}',
            hx_swap='outerHTML', target_id=tid)
    return fh.Li(toggle, delete, todo.title + (' ✅' if todo.done else ''),
            id=tid)

fapp, rt, todos, Todo = fh.fast_app(
        'data/todos.db',
        tbls={"todos":dict(id=int, title=str, render=render, done=bool, pk='id')})

def mk_input(): return fh.Input(placeholder='Add a new todo', id='title', hx_swap_oob='true')

@rt('/')
def get():
    frm = fh.Form(fh.Group(mk_input(), 
                    fh.Button("Add")),
            hx_post='/', target_id='todo-list', hx_swap='beforeend')
    return fh.Titled('Todos List',
                fh.Card(
                        fh.Ul(*todos(), id='todo-list'),
                        header=frm)
                )

@rt('/', methods=["post"])
def post(todo:Todo): return todos.insert(todo), mk_input()

@rt('/{tid}')
def delete(tid:int):todos.delete(tid)

@rt('/toggle/{tid}')
def get(tid:int):
    todo = todos[tid]
    todo.done = not todo.done
    return todos.update(todo)

@app.function(
    image=modal.Image.debian_slim(python_version="3.12").pip_install(
        "python-fasthtml==0.12.35"
    )
)
@modal.asgi_app()
def serve():
    return fapp
