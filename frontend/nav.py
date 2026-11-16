"""페이지 객체 레지스트리. app.py 가 채우고, 화면 코드는 st.switch_page(nav.PAGES[...]) 로 이동한다 (순환 import 방지)."""

PAGES: dict = {}
