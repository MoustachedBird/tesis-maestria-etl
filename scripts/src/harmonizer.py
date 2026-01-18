import polars as pl
import logging
import gc
from pathlib import Path
from scripts.src.reporter import ReportManager
from scripts.utils.matching import clean_contact_columns

# Importamos la refactorización
from scripts.utils.harmonizer_config import MEGA_REGIONS, SILVER_SCHEMA, COLS_NEEDED_FOR_MATCHING, TEXT_COLS_TO_SANITIZE
from scripts.utils.harmonizer_core import (
    get_region_prefix, 
    initialize_knowledge_base, 
    resolve_period_logic, 
    update_kb_logic, 
    consolidate_global_catalog_lazy
)

class DenueHarmonizer:
    def __init__(self, project_root: Path):
        self.root = project_root
        self.silver_dir = self.root / "data/processed/denue"
        self.curated_root = self.root / "data/curated"
        self.curated_dir = self.curated_root / "denue"
        self.master_catalog_path = self.curated_dir / "master_catalog.parquet"
        self.report_mgr = ReportManager(self.root / "data")
        
        # --- CORRECCIÓN: Asegurar que la ruta base existe antes del log ---
        self.curated_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.curated_dir / "harmonization_process.log"
        # -----------------------------------------------------------------
        
        # Parámetros
        self.spatial_tol = 60
        self.identity_thresh = 80
        self.activity_thresh = 60
        
        self._setup_logging()

    def _setup_logging(self):
        """Configura el logging asegurando que el directorio del archivo exista."""
        self.logger = logging.getLogger("Harmonizer")
        self.logger.setLevel(logging.INFO)
        
        if not self.logger.handlers:
            formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S')

            # Handler para Consola
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            # Handler para Archivo (Ubicado en data/curated/denue/)
            # El modo 'a' (append) permite continuar el log si el proceso se reinicia
            try:
                file_handler = logging.FileHandler(self.log_path, mode='a', encoding='utf-8')
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)
            except Exception as e:
                # Si falla el archivo, al menos imprimimos en consola el error
                print(f"⚠️ No se pudo crear el archivo de log en {self.log_path}: {e}")

    def get_processing_groups(self) -> list[list[str]]:
        """Detecta carpetas y arma los grupos."""
        available_states = set()
        for path in self.silver_dir.glob("state=*"):
            if path.is_dir():
                available_states.add(path.name.split("=")[1])
        
        groups = []
        handled_states = set()

        for region_name, states_in_region in MEGA_REGIONS.items():
            current_group = [s for s in states_in_region if s in available_states]
            if current_group:
                groups.append(current_group)
                self.logger.info(f"🔗 Mega-Región detectada ({region_name}): {current_group}")
                handled_states.update(current_group)

        for state in sorted(list(available_states)):
            if state not in handled_states:
                groups.append([state])
        return groups

    def load_group_history(self, state_list: list[str]) -> pl.DataFrame:
        """Carga IO, inyecta rutas de archivos y realiza limpieza inicial."""
        dfs = []
        
        # Definimos las columnas que sí existen físicamente en los archivos Silver
        # Excluimos 'source_paths' de la lectura inicial ya que la crearemos ahora
        cols_to_read = [c for c in COLS_NEEDED_FOR_MATCHING if c != 'source_paths']

        for state in state_list:
            try:
                state_path = self.silver_dir / f"state={state}"
                if not state_path.exists():
                    continue
                
                # Buscamos archivos parquet de forma individual para capturar su ruta
                parquet_files = list(state_path.glob("**/*.parquet"))
                if not parquet_files:
                    continue

                self.logger.info(f"   📂 Cargando: {state}")
                
                for file_path in parquet_files:
                    # Leemos el archivo inyectando su propia ruta como valor
                    df_part = pl.read_parquet(
                        file_path, 
                        columns=cols_to_read,
                        hive_partitioning=True
                    ).with_columns(
                        # Guardamos la ruta relativa al proyecto para trazabilidad limpia
                        pl.lit(str(file_path.relative_to(self.root))).alias("source_paths")
                    )
                    dfs.append(df_part)
                    
            except Exception as e:
                self.logger.error(f"❌ Error cargando {state}: {e}")

        if not dfs: return pl.DataFrame()

        # Concatenamos todos los archivos del grupo/región
        full_df = pl.concat(dfs)
        
        # Sanitización de nulos en columnas de texto
        full_df = full_df.with_columns([
            pl.col(c).fill_null("") for c in TEXT_COLS_TO_SANITIZE if c in full_df.columns
        ])
        
        return clean_contact_columns(full_df)

    def process_group(self, state_list: list[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Coordina la lógica Core."""
        prefix = get_region_prefix(state_list)
        self.logger.info(f"   🔑 Namespace de Región: {prefix}")
        
        df_group = self.load_group_history(state_list)
        if df_group.is_empty():
            return pl.DataFrame(), pl.DataFrame()

        # --- FIX ORDEN CRONOLÓGICO ---
        unique_periods = df_group['period'].unique().to_list()
        periods = sorted(
            unique_periods, 
            key=lambda x: (int(x.split('_')[1]), int(x.split('_')[0])), 
            reverse=True
        )

        self.logger.info(f"   📅 Periodos detectados: {periods}")
        
        dict_dfs = {p: df_group.filter(pl.col('period') == p) for p in periods}
        
        # --- Llamadas al Core ---
        self.logger.info(f"      🌱 Sembrando Knowledge Base (KB) con año base: {periods[0]}...")
        map_accum, kb = initialize_knowledge_base(dict_dfs[periods[0]], prefix)
        self.logger.info(f"         📚 Entidades iniciales en memoria: {kb.height:,}")
        
        for i in range(1, len(periods)):
            period = periods[i]
            target_rows = dict_dfs[period].height
            kb_before = kb.height # <--- Guardamos el tamaño actual antes de procesar
            
            # --- LOGS DE PROGRESO ---
            self.logger.info(f"      🔄 [{i}/{len(periods)-1}] Analizando periodo {period} ({target_rows:,} negocios)...")
            self.logger.info(f"         ↳ Propagando CLEEs del futuro hacia {period}...")
            
            # Delegamos la matemática a harmonizer_core
            df_resolved = resolve_period_logic(
                dict_dfs[period], kb, prefix, 
                self.spatial_tol, self.identity_thresh, self.activity_thresh
            )
            
            map_accum = map_accum.vstack(df_resolved.select(['row_id', 'master_clee']))
            
            # Actualización de memoria
            kb = update_kb_logic(kb, df_resolved)
            
            # --- CÁLCULO DE CRECIMIENTO ---
            new_entities = kb.height - kb_before
            self.logger.info(f"         🧠 Memoria actualizada con {period}:")
            self.logger.info(f"            📈 Nuevas entidades (no estaban en el futuro): +{new_entities:,}")
            self.logger.info(f"            📚 Total acumulado en KB: {kb.height:,} registros")
            self.logger.info(f"         ✅ {period} Terminado.")
            
        return map_accum, kb

    def run(self, incremental: bool = True):
        self.curated_root.mkdir(parents=True, exist_ok=True)
        
        # --- SISTEMA DE SNAPSHOTS ---
        snapshot_dir = self.curated_dir / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        # ----------------------------

        all_groups = self.get_processing_groups()
        
        if incremental:
            processed_partitions = self.report_mgr.process_report.get_harmonized_partitions()
            groups_to_process = []
            for group in all_groups:
                is_group_done = True
                for state in group:
                    state_path = f"state={state}"
                    if not any(p.startswith(state_path) for p in processed_partitions):
                        is_group_done = False
                        break
                if not is_group_done:
                    groups_to_process.append(group)
        else:
            groups_to_process = all_groups

        if not groups_to_process:
            self.logger.info("Nothing to harmonize.")
            return

        for i, group in enumerate(groups_to_process, 1):
            prefix = get_region_prefix(group)
            group_name = "+".join(group)
            self.logger.info(f"🚀 [{i}/{len(groups_to_process)}] Procesando: {group_name}")
            
            group_map, group_kb = self.process_group(group)
            
            if not group_map.is_empty():
                updated_paths = self._save_local_results(group, group_map)
                
                # --- GUARDAR SNAPSHOT ---
                snapshot_path = snapshot_dir / f"kb_{prefix}.parquet"
                group_kb.write_parquet(snapshot_path)
                self.logger.info(f"   📸 Snapshot guardado: {snapshot_path.name}")
                # ------------------------

                if updated_paths:
                    status_updates = [{"partition_path": p, "status": "HARMONIZED"} for p in updated_paths]
                    self.report_mgr.process_report.update_harmonization_status(status_updates)
            
            del group_map
            del group_kb
            gc.collect()

        # --- CONSOLIDACIÓN FINAL DESDE DISCO ---
        self.logger.info("📚 Consolidando Master Catalog desde snapshots...")
        self._consolidate_snapshots(snapshot_dir)
        
        self.report_mgr.update_harmonization_status([{"partition_path": "GLOBAL", "status": "HARMONIZED"}])
        self.logger.info("✨ Proceso Finalizado.")

    def _consolidate_snapshots(self, snapshot_dir: Path):
        """Lee snapshots y genera el catálogo maestro usando lógica Lazy."""
        snapshot_files = list(snapshot_dir.glob("kb_*.parquet"))
        
        if not snapshot_files:
            self.logger.warning("⚠️ No se encontraron snapshots.")
            return

        self.logger.info(f"   📦 Uniendo {len(snapshot_files)} archivos regionales...")
                
        lazy_frames = [pl.scan_parquet(f) for f in snapshot_files]
        master_kb = consolidate_global_catalog_lazy(lazy_frames).collect()
        
        master_kb.write_parquet(self.master_catalog_path)
        self.logger.info(f"🏆 Catálogo Maestro Guardado: {master_kb.height} entidades únicas.")

    def _save_local_results(self, group: list[str], group_map: pl.DataFrame) -> list[str]:
        """Helper privado. Retorna lista de partition_paths relativos actualizados."""
        lz_map = group_map.lazy()
        target_files = []
        for state in group:
            state_path = self.silver_dir / f"state={state}"
            target_files.extend(list(state_path.glob("**/*.parquet")))
        
        updated_paths = []

        for file_path in target_files:
            try:
                rel_path = file_path.relative_to(self.silver_dir) # path para manifest
                dest_path = self.curated_dir / rel_path
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                df_curated = pl.read_parquet(file_path).drop('clee').lazy().join(
                    lz_map, on='row_id', how='left'
                ).rename({'master_clee': 'clee'}).collect()
                
                cols = ['clee'] + [c for c in df_curated.columns if c != 'clee']
                df_curated.select(cols).write_parquet(dest_path, compression='snappy')
                
                full_rel_path = str(file_path.relative_to(self.root)) # data/processed/denue/...
                updated_paths.append(full_rel_path)

            except Exception as e:
                self.logger.error(f"Error escribiendo {file_path.name}: {e}")
        
        return updated_paths

if __name__ == "__main__":
    harmonizer = DenueHarmonizer(Path.cwd())
    harmonizer.run()