import polars as pl
import logging
import gc
from pathlib import Path
from scripts.utils.manifest import ManifestManager
from scripts.utils.matching import clean_contact_columns

# Importamos la refactorización
from scripts.utils.harmonizer_config import MEGA_REGIONS, SILVER_SCHEMA, COLS_NEEDED_FOR_MATCHING, TEXT_COLS_TO_SANITIZE
from scripts.utils.harmonizer_core import (
    get_region_prefix, 
    initialize_knowledge_base, 
    resolve_period_logic, 
    update_kb_logic, 
    consolidate_global_catalog
)

class DenueHarmonizer:
    def __init__(self, project_root: Path):
        self.root = project_root
        self.silver_dir = self.root / "data/processed/denue"
        self.curated_root = self.root / "data/curated"
        self.curated_dir = self.curated_root / "denue"
        self.master_catalog_path = self.curated_dir / "master_catalog.parquet"
        self.manifest_mgr = ManifestManager(self.root / "data")
        
        # Parámetros
        self.spatial_tol = 60
        self.identity_thresh = 80
        self.activity_thresh = 60
        
        self._setup_logging()

    def _setup_logging(self):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
        self.logger = logging.getLogger("Harmonizer")

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
        """Carga IO y limpieza inicial."""
        dfs = []
        for state in state_list:
            try:
                state_path = self.silver_dir / f"state={state}"
                if not state_path.exists() or not any(state_path.glob("**/*.parquet")): 
                    continue
                
                self.logger.info(f"   📂 Cargando: {state}")
                q = pl.scan_parquet(state_path / "**/*.parquet", hive_partitioning=True, schema=SILVER_SCHEMA)
                
                df_part = q.select(COLS_NEEDED_FOR_MATCHING).filter(
                    pl.col('latitud').is_not_null() & pl.col('longitud').is_not_null()
                ).collect()
                dfs.append(df_part)
            except Exception as e:
                self.logger.error(f"❌ Error cargando {state}: {e}")

        if not dfs: return pl.DataFrame()

        full_df = pl.concat(dfs)
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

        periods = sorted(df_group['period'].unique().to_list(), reverse=True)
        self.logger.info(f"   📅 Periodos detectados: {periods}")
        
        dict_dfs = {p: df_group.filter(pl.col('period') == p) for p in periods}
        
        # --- Llamadas al Core ---
        map_accum, kb = initialize_knowledge_base(dict_dfs[periods[0]], prefix)
        
        for i in range(1, len(periods)):
            period = periods[i]
            # Delegamos la matemática a harmonizer_core
            df_resolved = resolve_period_logic(
                dict_dfs[period], kb, prefix, 
                self.spatial_tol, self.identity_thresh, self.activity_thresh
            )
            map_accum = map_accum.vstack(df_resolved.select(['row_id', 'master_clee']))
            kb = update_kb_logic(kb, df_resolved)
            
        return map_accum, kb

    def run(self, incremental: bool = True):
        """Punto de entrada con soporte Incremental."""
        self.curated_root.mkdir(parents=True, exist_ok=True)
        
        # 1. Detectar Grupos Físicos (Disco)
        all_groups = self.get_processing_groups()
        
        # 2. Lógica Incremental: ¿Qué procesamos?
        groups_to_process = []
        
        if incremental:
            # Preguntamos al manifiesto qué está pendiente (NEW o ERROR)
            tasks = self.manifest_mgr.get_harmonization_tasks(retry_errors=True)
            
            if not tasks:
                self.logger.info("✨ Todo está armonizado. Nada que hacer.")
                return

            # Extraemos los estados que necesitan actualización
            # tasks es lista de dicts con keys: 'state', 'partition_path', etc.
            pending_states = set(t['state'] for t in tasks if 'state' in t)
            
            if not pending_states:
                # Fallback si el manifiesto no tiene columna state
                self.logger.warning("⚠️ No se detectaron estados en las tareas. Ejecutando todo.")
                groups_to_process = all_groups
            else:
                self.logger.info(f"🎯 Estados pendientes: {pending_states}")
                # Filtramos los grupos: Si UN estado del grupo está pendiente, procesamos TODO el grupo
                for group in all_groups:
                    # Intersección: si hay elementos en común entre el grupo y los pendientes
                    if not set(group).isdisjoint(pending_states):
                        groups_to_process.append(group)
        else:
            # Modo Fuerza Bruta
            groups_to_process = all_groups

        self.logger.info(f"🌎 Procesando {len(groups_to_process)} de {len(all_groups)} grupos.")
        
        global_catalog_buffer = [] 
        processed_partitions = [] # Para actualizar el manifiesto al final

        # 3. Loop de Procesamiento
        for i, group in enumerate(groups_to_process, 1):
            group_name = "+".join(group) if len(group) > 1 else group[0]
            self.logger.info(f"🚀 [{i}/{len(groups_to_process)}] Procesando: {group_name}")
            
            group_map, group_kb = self.process_group(group)
            
            if not group_map.is_empty():
                self.logger.info(f"   💾 Escribiendo resultados locales...")
                # Guardamos y obtenemos qué archivos tocamos para actualizar el manifiesto
                affected_files = self._save_local_results(group, group_map)
                processed_partitions.extend(affected_files)
                
                global_catalog_buffer.append(group_kb)
            
            del group_map
            del group_kb
            gc.collect()
            
        # 4. Consolidación (Solo si hubo cambios, o si forzamos regeneración)
        # Nota: Idealmente deberíamos cargar el Master existente y hacer merge, 
        # pero para tu tesis, regenerarlo con lo nuevo es aceptable si procesas todo,
        # o si procesas parcial, deberías leer el master actual.
        # POR AHORA: Si es incremental, asumimos que quieres reconstruir el master 
        # con lo que acabas de procesar + lo que ya existía (Complejo).
        # SIMPLIFICACIÓN: El Master Catalog se reconstruye mejor corriendo un FULL run periódicamente.
        
        if global_catalog_buffer:
            self.logger.info("📚 Actualizando Master Catalog (Parcial)...")
            # Cuidado: Esto sobrescribe el master solo con lo procesado hoy.
            # Para producción real necesitarías leer el master viejo y unir.
            # Para tesis: Asumiremos que corres 'make etl-harmonize' (Full) la primera vez
            master_kb = consolidate_global_catalog(global_catalog_buffer)
            master_kb.write_parquet(self.master_catalog_path)
            self.logger.info(f"🏆 Catálogo Maestro Guardado.")
        
        # 5. Actualizar Manifiesto
        if processed_partitions:
            self.logger.info(f"📝 Actualizando estado de {len(processed_partitions)} particiones...")
            updates = [{"partition_path": p, "status": "HARMONIZED"} for p in processed_partitions]
            self.manifest_mgr.update_harmonization_status(updates)

        self.logger.info("✨ Proceso Finalizado.")

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
                
                # Agregamos a la lista de actualizados
                # Ojo: El manifiesto usa paths relativos a PROJECT_ROOT/data/processed/denue?
                # Revisando manifest.py, usa relative_to(project_root).
                # Ajustamos para coincidir con lo que espera manifest.py
                full_rel_path = str(file_path.relative_to(self.root)) # data/processed/denue/...
                updated_paths.append(full_rel_path)

            except Exception as e:
                self.logger.error(f"Error escribiendo {file_path.name}: {e}")
        
        return updated_paths
if __name__ == "__main__":
    harmonizer = DenueHarmonizer(Path.cwd())
    harmonizer.run()