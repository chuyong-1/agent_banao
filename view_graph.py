from graph import app

# Generate a Mermaid.js flowchart of your pipeline
print(app.get_graph().draw_mermaid())