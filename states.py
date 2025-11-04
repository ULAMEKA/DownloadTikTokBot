from aiogram.fsm.state import State, StatesGroup

class DownloadVideo(StatesGroup):
    waiting_for_link = State()
    waiting_for_quality = State()
