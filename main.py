import os, shutil, tempfile, zipfile, json
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import geopandas as gpd
from sqlalchemy import create_engine, text
import asyncpg
from dotenv import load_dotenv

# --- Config ---
load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_db_url():
    url = os.environ.get("DATABASE_URL")
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url or "postgresql://postgres:4721040073@localhost:5432/webgis_db" # Default for local

DB_URL = get_db_url()
engine = create_engine(DB_URL)

app = FastAPI(title="Professional WebGIS")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

async def get_db_conn():
    try:
        return await asyncpg.connect(DB_URL)
    except Exception as e:
        print(f"❌ Database Connection Error: {e}")
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

class BufferRequest(BaseModel):
    table_name: str
    distance: float # in meters

# --- API Endpoints ---
@app.get("/api/test-db")
async def test_db_connection():
    try:
        conn = await get_db_conn()
        postgis_version = await conn.fetchval("SELECT PostGIS_Full_Version()")
        await conn.close()
        return {"status": "success", "message": "Database and PostGIS are connected!", "postgis_version": postgis_version}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to database: {e}")

@app.get("/api/layers")
async def get_all_layers():
    conn = await get_db_conn()
    query = """
        SELECT f_table_name AS name, type AS geom_type, srid 
        FROM geometry_columns 
        WHERE f_table_schema = 'public'
        ORDER BY name;
    """
    rows = await conn.fetch(query)
    await conn.close()
    return rows

@app.get("/api/layers/{table_name}/geojson")
async def get_layer_geojson(table_name: str):
    conn = await get_db_conn()
    # ดึงข้อมูลพร้อม properties ทั้งหมด
    query = f"""
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(json_agg(ST_AsGeoJSON(t.*)::json), '[]')
        ) FROM "{table_name}" AS t;
    """
    result = await conn.fetchval(query)
    await conn.close()
    return json.loads(result) if result else {"type": "FeatureCollection", "features": []}

@app.get("/api/layers/{table_name}/attributes")
async def get_layer_attributes(table_name: str):
    conn = await get_db_conn()
    # ดึงข้อมูล Attribute ทั้งหมด (ยกเว้น geometry)
    # ค้นหาชื่อคอลัมน์ที่ไม่ใช่ geometry
    geom_column = await conn.fetchval(f"SELECT f_geometry_column FROM geometry_columns WHERE f_table_name = $1 AND f_table_schema = 'public'", table_name)
    
    if not geom_column:
        raise HTTPException(status_code=404, detail="Table or geometry column not found.")

    columns = await conn.fetch(f"SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1 AND column_name != $2", table_name, geom_column)
    column_names = [col['column_name'] for col in columns]
    
    if not column_names: # ถ้าไม่มีคอลัมน์อื่นนอกจาก geometry
        rows = await conn.fetch(f"SELECT * FROM \"{table_name}\" LIMIT 100") # ดึงมาแค่ 100 แถว
        data = [dict(r) for r in rows]
    else:
        query_cols = ", ".join([f'"{c}"' for c in column_names])
        rows = await conn.fetch(f"SELECT {query_cols} FROM \"{table_name}\" LIMIT 100") # ดึงมาแค่ 100 แถว
        data = [dict(r) for r in rows]

    await conn.close()
    return {"headers": column_names, "data": data}


@app.post("/api/upload")
async def upload_shapefile(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = os.path.join(tmp_dir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        try:
            target_path = file_path
            if file.filename.endswith(".zip"):
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(tmp_dir)
                shp_files = [f for f in os.listdir(tmp_dir) if f.endswith(".shp")]
                if not shp_files:
                    raise HTTPException(status_code=400, detail="Zip file must contain a .shp file")
                target_path = os.path.join(tmp_dir, shp_files[0])
            elif not file.filename.lower().endswith(('.shp', '.geojson', '.json')):
                raise HTTPException(status_code=400, detail="Unsupported file type. Please upload .zip (Shapefile) or .geojson/.json")

            gdf = gpd.read_file(target_path)
            if gdf.crs is None:
                gdf.set_crs(epsg=4326, inplace=True) # Assume WGS84 if no CRS
            else:
                gdf = gdf.to_crs(epsg=4326) # Always convert to WGS84 for PostGIS

            table_name = os.path.splitext(file.filename)[0].lower().replace(" ", "_").replace("-", "_")
            gdf.to_postgis(name=table_name, con=engine, if_exists='replace', index=False)
            return {"message": f"Layer '{table_name}' uploaded successfully."}
        except Exception as e:
            print(f"Upload Error: {e}")
            raise HTTPException(status_code=500, detail=f"File processing failed: {str(e)}")

@app.get("/api/export/{table_name}/shapefile")
async def export_shapefile(table_name: str):
    try:
        sql_query = f'SELECT * FROM "{table_name}"'
        gdf = gpd.read_postgis(sql_query, con=engine, geom_col='geom')

        if gdf.empty:
            raise HTTPException(status_code=404, detail="Layer not found or empty.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_shp_path = os.path.join(tmp_dir, f"{table_name}.shp")
            gdf.to_file(output_shp_path, driver='ESRI Shapefile')

            zip_file_name = f"{table_name}_export.zip"
            zip_path = os.path.join(tmp_dir, zip_file_name)
            
            # Create a zip file containing all shapefile components
            with zipfile.ZipFile(zip_path, 'w') as zf:
                for f in os.listdir(tmp_dir):
                    if f.startswith(table_name): # Include all components like .shp, .shx, .dbf, .prj
                        zf.write(os.path.join(tmp_dir, f), arcname=f)
            
            return FileResponse(path=zip_path, filename=zip_file_name, media_type="application/zip")
    except Exception as e:
        print(f"Export Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to export shapefile: {str(e)}")

@app.post("/api/analysis/buffer")
async def run_buffer_analysis(params: BufferRequest):
    new_table_name = f"{params.table_name}_buffer_{int(params.distance)}m"
    try:
        with engine.connect() as conn:
            # Drop existing buffer table if it exists
            conn.execute(text(f'DROP TABLE IF EXISTS "{new_table_name}"'))
            # Create buffer (using geography for accurate distance in meters)
            query = f"""
                CREATE TABLE "{new_table_name}" AS
                SELECT ST_Transform(ST_Buffer(ST_Transform(geom, 3857), {params.distance}), 4326) AS geom, 
                       id -- Assuming 'id' column exists, adjust if needed
                FROM "{params.table_name}";
            """
            # ถ้าไม่มีคอลัมน์ 'id' ให้แก้ไข query เป็น 'SELECT ST_Transform(...), 1 as id FROM ...'
            conn.execute(text(query))
            conn.commit()
        return {"status": "success", "new_layer_name": new_table_name, "message": f"Buffer created as '{new_table_name}'"}
    except Exception as e:
        print(f"Buffer Analysis Error: {e}")
        raise HTTPException(status_code=500, detail=f"Buffer analysis failed: {str(e)}")


# --- Serve Frontend ---
if os.path.exists(os.path.join(BASE_DIR, "index.html")):
    app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")