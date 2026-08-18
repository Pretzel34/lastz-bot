import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from gui import BotApp

app = BotApp()
app.after(1000, app._toggle_record_runs)  # resume Record Runs (was on before restart)
app.after(2000, app._start_all)
app.mainloop()
