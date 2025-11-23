from fasthtml.common import *
#from fasthtml import common as fh

def render(todo):
    tid = f'todo-{todo.id}'
    toggle = A('Toggle', hx_get=f'/toggle/{todo.id}', target_id=tid)
    delete = A('Delete', hx_delete=f'/{todo.id}',
               hx_swap='outerHTML', target_id=tid)
    return Li(toggle, delete, todo.title + (' ✅' if todo.done else ''),
              id=tid)

app, rt, todos, Todo = fast_app(
        'todos.db', live=True, 
        tbls={"todos":dict(id=int, title=str, render=render, done=bool, pk='id')})

@rt('/')
def get():
    #todos.insert(Todo(title="second todo", done=False))
    #items = [Li(o) for o in todos()]
    return Titled('Todos List',
                  Ul(*todos()),
                  )

@rt('/{tid}')
def delete(tid:int):todos.delete(tid)

@rt('/toggle/{tid}')
def get(tid:int):
    todo = todos[tid]
    todo.done = not todo.done
    return todos.update(todo)

serve()
