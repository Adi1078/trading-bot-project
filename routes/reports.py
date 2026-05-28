from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from reports.generator import generate_monthly_report, generate_monthly_report_html

router = APIRouter()


@router.get("/")
def get_report(year: int, month: int):
    """Get monthly report as JSON for the admin panel."""
    return generate_monthly_report(year, month)


@router.get("/download")
def download_report(year: int, month: int):
    """Download monthly report as an HTML file."""
    html = generate_monthly_report_html(year, month)
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f"attachment; filename=report_{year}_{month}.html"}
    )
