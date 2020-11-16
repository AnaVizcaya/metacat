import uuid, json, hashlib, re, time, io, traceback
from metacat.util import to_bytes, to_str, epoch, PasswordHashAlgorithm
from psycopg2 import IntegrityError

Debug = False

def debug(*parts):
    if Debug:
        print(*parts)
        
Aliases = {}
def alias(prefix="t"):
    global Aliases
    i = Aliases.get(prefix, 1)
    Aliases[prefix] = i+1
    return f"{prefix}_{i}"

class AlreadyExistsError(Exception):
    pass

class NotFoundError(Exception):
    def __init__(self, msg):
        self.Message = msg

    def __str__(self):
        return "Not found error: %s" % (self.Message,)
        
def parse_name(name, default_namespace):
    words = name.split(":", 1)
    if len(words) < 2 or not words[0]:
        assert not not default_namespace, "Null default namespace"
        ns = default_namespace
        name = words[-1]
    else:
        ns, name = words
    return ns, name
                

def fetch_generator(c):
    while True:
        tup = c.fetchone()
        if tup is None: break
        yield tup
        
def first_not_empty(lst):
    val = None
    for v in lst:
        val = v
        if v is not None and not (isinstance(v, list) and len(v) == 0):
            return v
    else:
        return val
        
def limited(iterable, n):
    for f in iterable:
        if n is None:
            yield f
        else:
            if n or n > 0:
                yield f
            else:
                break
            n -= 1
            
class DBFileSet(object):
    
    def __init__(self, db, files=[], limit=None):
        self.DB = db
        self.Files = files
        self.Limit = limit
        self.SQL = None

    def limit(self, n):
        return DBFileSet(self.DB, self.Files, n)
        
    @staticmethod
    def from_tuples(db, g):
        # must be in sync with DBFile.all_columns()
        return DBFileSet(db, 
            (
                DBFile.from_tuple(db, t) for t in g
            )
        )
        
    @staticmethod
    def from_id_list(db, lst):
        c = db.cursor()
        columns = DBFile.all_columns()
        c.execute(f"""
            select {columns}
                where id = any(%s)""", (list(lst),))
        return DBFileSet.from_tuples(db, fetch_generator(c))
    
    @staticmethod
    def from_name_list(db, names, default_namespace=None):
        full_names = [parse_name(x, default_namespace) for x in names]
        just_names = [name for ns, name in full_names]
        joined = set("%s:%s" % t for t in full_names)
        c = db.cursor()
        columns = DBFile.all_columns()
        c.execute(f"""
            select {columns}, null as parents, null as children from files
                where name = any(%s)""", (just_names,))
        selected = ((fid, namespace, name, metadata) 
                    for (fid, namespace, name, metadata) in fetch_generator(c)
                    if "%s:%s" % (namespace, name) in joined)
        return DBFileSet.from_tuples(db, selected)
        
    def __iter__(self):
        return limited(self.Files, self.Limit)
                        
    def as_list(self):
        # list(DBFileSet) should work too
        return list(self.Files)
            
    def parents(self, with_metadata = False, with_provenance = False):
        return self._relationship("parents", with_metadata, with_provenance)
            
    def children(self, with_metadata = False, with_provenance = False):
        return self._relationship("children", with_metadata, with_provenance)
            
    def _relationship(self, rel, with_metadata, with_provenance):
        table = "files" if not with_provenance else "files_with_provenance"
        f = alias("f")
        pc = alias("pc")
        attrs = DBFile.attr_columns(f)
        if rel == "children":
            join = f"{f}.id = {pc}.child_id and {pc}.parent_id = any (%s)"
        else:
            join = f"{f}.id = {pc}.parent_id and {pc}.child_id = any (%s)"
            
        meta = "null as metadata" if not with_metadata else f"{f}.metadata"
        provenance = "null as parents, null as children" if not with_provenance else \
            f"{f}.parents, {f}.children"
            
        c = self.DB.cursor()
        file_ids = list(f.FID for f in self.Files)

        sql = f"""select distinct {f}.id, {f}.namespace, {f}.name, {meta}, {attrs}, {provenance}
                    from {table} {f}, parent_child {pc}
                    where {join}
                    """
        c.execute(sql, (file_ids,))
        return DBFileSet.from_tuples(self.DB, fetch_generator(c))

    @staticmethod
    def join(db, file_sets):
        first = file_sets[0]
        if len(file_sets) == 1:
            return first
        file_list = list(first)
        file_ids = set(f.FID for f in file_list)
        for another in file_sets[1:]:
            another_ids = set(f.FID for f in another)
            file_ids &= another_ids
        return DBFileSet(db, (f for f in file_list if f.FID in file_ids))

    @staticmethod
    def union(db, file_sets):
        def union_generator(file_lists):
            file_ids = set()
            for lst in file_lists:
                #print("DBFileSet.union: lst:", lst)
                for f in lst:
                    if not f.FID in file_ids:
                        file_ids.add(f.FID)
                        yield f
        gen = union_generator(file_sets)
        #print("DBFileSet.union: returning:", gen)
        return DBFileSet(db, gen)

    def subtract(self, right):
        right_ids = set(f.FID for f in right)
        #print("DBFileSet: right_ids:", len(right_ids))
        return DBFileSet(self.DB, (f for f in self if not f.FID in right_ids))
        
    __sub__ = subtract
    
    @staticmethod
    def from_basic_query(db, basic_file_query, with_metadata, limit):
        
        debug("from_basic_query: with_metadata:", with_metadata)
        
        if limit is None:
            limit = basic_file_query.Limit
        elif basic_file_query.Limit is not None:
            limit = min(limit, basic_file_query.Limit)
            
        dataset_selector = basic_file_query.DatasetSelector
        datasets = None
        if dataset_selector is not None:
            datasets = list(basic_file_query.DatasetSelector.datasets(db))
            if not datasets:
                return DBFileSet(db)      # empty File Set

        if dataset_selector is None:
            return DBFileSet.all_files(db, dnf, with_metadata, limit)
            
        elif len(datasets) == 1:
            return datasets[0].list_files(with_metadata = with_metadata, condition=basic_file_query.Wheres, limit=limit,
                        relationship = basic_file_query.Relationship)
        else:
            return DBFileSet.union(
                        ds.list_files(
                            with_metadata = with_metadata, condition=basic_file_query.Wheres,
                            relationship = basic_file_query.Relationship, limit=limit
                        )
                        for ds in datasets
            )
            
    @staticmethod
    def sql_for_basic_query(basic_file_query):
        limit = basic_file_query.Limit
        limit = "" if limit is None else f"limit {limit}"
        
        f = alias("f")

        meta = f"{f}.metadata" if basic_file_query.WithMeta else "null as metadata"
        parents = f"{f}.parents" if basic_file_query.WithProvenance else "null as parents"
        children = f"{f}.children" if basic_file_query.WithProvenance else "null as children"
        table = "files_with_provenance" if basic_file_query.WithProvenance else "files"
        
        where_exp = MetaExpressionDNF(basic_file_query.Wheres).sql(f)
        meta_where_clause = f"where {where_exp}" if where_exp else ""
        

        dataset_selector = basic_file_query.DatasetSelector
        attrs = DBFile.attr_columns(f)
        if dataset_selector is None:
            # no dataset selection
            sql = f"""
                -- sql_for_basic_query {f}
                    select {f}.id, {f}.namespace, {f}.name, {meta}, {attrs}, {parents}, {children}
                        from {table} {f}
                        {meta_where_clause}
                        {limit}
                -- end of sql_for_basic_query {f}
            """
        else:
            datasets_sql = DBDataset.sql_for_selector(dataset_selector)
        
            fd = alias("fd")
            ds = alias("ds")
        
            sql = f"""
                -- sql_for_basic_query {f}
                    with selected_datasets as (
                        {datasets_sql}
                    )
                    select {f}.id, {f}.namespace, {f}.name, {meta}, {attrs}, {parents}, {children}
                        from {table} {f}
                            inner join files_datasets {fd} on {fd}.file_id = {f}.id
                            inner join selected_datasets on 
                                selected_datasets.namespace = {fd}.dataset_namespace 
                                and selected_datasets.name = {fd}.dataset_name 
                        {meta_where_clause}
                        {limit}
                -- end of sql_for_basic_query {f}
            """
        return sql
        
    @staticmethod
    def sql_for_file_list(spec_list, with_meta, with_provenance, limit):
        f = alias("f")
        meta = f"{f}.metadata" if with_meta else "null as metadata"
        ids = []
        specs = []
        
        for s in spec_list:
            if ':' in s:
                specs.append(s)
            else:
                ids.append(s)
                
        debug("sql_for_file_list: specs, ids:", specs, ids)
                
        ids_part = ""
        specs_part = ""
        
        parts = []
        
        attrs = DBFile.attr_columns(f)

        if with_provenance:
            table = "files_with_provenance"
            prov_columns = f"{f}.parents, {f}.children"
        else:
            table = "files"
            prov_columns = f"null as parents, null as children"
        
        if ids:
            id_list = ",".join(["'%s'" % (i,) for i in ids])
            ids_part = f"""
                select {f}.id, {f}.namespace, {f}.name, {meta}, {prov_columns}, {attrs} from {table} {f}
                    where id in ({id_list})
                """
            parts.append(ids_part)
        
        if specs:
            parsed = [s.split(":",1) for s in specs]
            namespaces, names = zip(*parsed)
            namespaces = list(set(namespaces))
            assert not "" in namespaces
            names = list(set(names))
            
            namespaces = ",".join([f"'{ns}'" for ns in namespaces])
            names = ",".join([f"'{n}'" for n in names])
            specs = ",".join([f"'{s}'" for s in specs])
            
            specs_part = f"""
                select {f}.id, {f}.namespace, {f}.name, {meta}, {prov_columns}, {attrs} from {table} {f}
                    where {f}.name in ({names}) and {f}.namespace in ({namespaces}) and
                         {f}.namespace || ':' || {f}.name in ({specs})
            """
            parts.append(specs_part)

        return "\nunion\n".join(parts)

    @staticmethod
    def from_sql(db, sql):
        c = db.cursor()
        c.execute(sql)
        fs = DBFileSet.from_tuples(db, fetch_generator(c))
        fs.SQL = sql
        return fs
    

        
class DBFile(object):
    
    ColumnAttributes=[      # column names which can be used in queries
        "creator", "created_timestamp", "name", "namespace", "size"
    ]  
    def __init__(self, db, namespace = None, name = None, metadata = None, fid = None, size=None, checksums=None,
                    parents = None, children = None, creator = None, created_timestamp=None,
                    ):
        assert (namespace is None) == (name is None)
        self.DB = db
        self.FID = fid or uuid.uuid4().hex
        self.FixedFID = (fid is not None)
        self.Namespace = namespace
        self.Name = name
        self.Metadata = metadata
        self.Creator = creator
        self.CreatedTimestamp = created_timestamp
        self.Checksums = checksums
        self.Size = size
        self.Parents = parents
        self.Children = children
    
    ID_BITS = 64
    ID_NHEX = ID_BITS/4
    ID_FMT = f"%0{ID_NHEX}x"
    ID_MASK = (1<<ID_BITS) - 1
    
    def generate_id(self):          # not used. Use 128 bit uuid instead to guarantee uniqueness
        x = uuid.uuid4().int
        fid = 0
        while x:
            fid ^= (x & self.ID_MASK)
            x >>= self.ID_BITS
        return self.ID_FMT % fid
        
    def __str__(self):
        return "[DBFile %s %s:%s]" % (self.FID, self.Namespace, self.Name)
        
    __repr__ = __str__

    CoreColumnNames = [
        "id", "namespace", "name", "metadata"
    ]
    
    AttrColumnNames = [
        "creator", "created_timestamp", "size", "checksums"
    ]

    AllColumnNames = CoreColumnNames + AttrColumnNames

    @staticmethod
    def all_columns(alias=None, with_meta=False):
        if alias:
            return ','.join(f"{alias}.{c}" for c in DBFile.AllColumnNames)
        else:
            return ','.join(DBFile.AllColumnNames)

    @staticmethod
    def attr_columns(alias=None):
        if alias:
            return ','.join(f"{alias}.{c}" for c in DBFile.AttrColumnNames)
        else:
            return ','.join(DBFile.AttrColumnNames)

    def create(self, creator=None, do_commit = True):
        from psycopg2 import IntegrityError
        c = self.DB.cursor()
        try:
            meta = json.dumps(self.Metadata or {})
            checksums = json.dumps(self.Checksums or {})
            c.execute("""
                insert into files(id, namespace, name, metadata, size, checksums, creator) values(%s, %s, %s, %s, %s, %s, %s)
                """,
                (self.FID, self.Namespace, self.Name, meta, self.Size, checksums, creator))
            if self.Parents:
                c.executemany(f"""
                    insert into parent_child(parent_id, child_id) values(%s, %s)
                """, [(p.FID if isinstance(p, DBFile) else p, self.FID) for p in self.Parents])
            if do_commit:   c.execute("commit")
        except IntegrityError:
            c.execute("rollback")
            raise AlreadyExistsError("%s:%s" % (self.Namespace, self.Name))
        except:
            c.execute("rollback")
            raise
        return self


    @staticmethod
    def create_many(db, files, creator=None, do_commit=True):
        files = list(files)
        files_csv = []
        parents_csv = []
        null = r"\N"
        for f in files:
            f.FID = f.FID or self.generate_id()
            files_csv.append("%s\t%s\t%s\t%s\t%s\t%s\t%s" % (
                f.FID,
                f.Namespace or null, 
                f.Name or null,
                json.dumps(f.Metadata) if f.Metadata else '{}',
                f.Size if f.Size is not None else null,
                json.dumps(f.Checksums) if f.Checksums else '{}',
                f.Creator or creator or null
            ))
            f.Creator = f.Creator or creator
            if f.Parents:
                parents_csv += ["%s\t%s" % (f.FID, p.FID if isinstance(p, DBFile) else p) for p in f.Parents]
            f.DB = db
        
        c = db.cursor()
        c.execute("begin")

        try:
            files_data = "\n".join(files_csv)
            #open("/tmp/files.csv", "w").write(files_data)
            c.copy_from(io.StringIO("\n".join(files_csv)), "files", 
                    columns = ["id", "namespace", "name", "metadata", "size", "checksums","creator"])
            c.copy_from(io.StringIO("\n".join(parents_csv)), "parent_child", 
                    columns=["child_id", "parent_id"])
            if do_commit:   c.execute("commit")
        except Exception as e:
            print(traceback.format_exc())
            c.execute("rollback")
            raise
            
        return DBFileSet(db, files)

        
    def update(self, do_commit = True):
        from psycopg2 import IntegrityError
        c = self.DB.cursor()
        meta = json.dumps(self.Metadata or {})
        checksums = json.dumps(self.Checksums or {})
        try:
            c.execute("""
                update files set namespace=%s, name=%s, metadata=%s, size=%s, checksums=%s where id = %s
                """, (self.Namespace, self.Name, meta, self.Size, checksums, self.FID)
            )
            if do_commit:   c.execute("commit")
        except:
            c.execute("rollback")
            raise    
        return self
        
    @staticmethod
    def from_tuple(db, tup):
        #print("----DBFile.from_tup: tup:", tup)
        if tup is None: return None
        try:    
            fid, namespace, name, meta, creator, created_timestamp, size, checksums, parents, children = tup
            f = DBFile(db, fid=fid, namespace=namespace, name=name, metadata=meta, size=size, checksums = checksums,
                parents = parents, children=children)
        except: 
            try:    
                fid, namespace, name, meta, creator, created_timestamp, size, checksums = tup
                f = DBFile(db, fid=fid, namespace=namespace, name=name, metadata=meta, size=size, checksums = checksums)
            except: 
                try:    
                    fid, namespace, name, meta = tup
                    f = DBFile(db, fid=fid, namespace=namespace, name=name, metadata=meta)
                except: 
                        fid, namespace, name = tup
                        f = DBFile(db, fid=fid, namespace=namespace, name=name)
        return f

    @staticmethod
    def update_many(db, files, do_commit=True):
        from psycopg2 import IntegrityError
        tuples = [
            (f.Namespace, f.Name, json.dumps(f.Metadata or {}), f.Size, json.dumps(f.Checksums or {}), f.FID)
            for f in files
        ]
        #print("tuples:", tuples)
        c = db.cursor()
        try:
            c.executemany("""
                update files
                    set namespace=%s, name=%s, metadata=%s, size=%s, checksums=%s
                    where id=%s
                """,
                tuples)
            if do_commit:   c.execute("commit")
        except:
            c.execute("rollback")
            raise
        for f in files: f.DB = db
    
    @staticmethod
    def get_files(db, files, load_all=False):
        c = db.cursor()
        strio = io.StringIO()
        for f in files:
            strio.write("%s\t%s\t%s\n" % (f.get("fid") or r'\N', f.get("namespace") or r'\N', f.get("name") or r'\N'))
        c.execute("""create temp table if not exists
            temp_files (
                id text,
                namespace text,
                name text)
                """)
        c.copy_from(io.StringIO(strio.getvalue()), "temp_files")
        
        columns = DBFile.all_columns("f")

        return DBFileSet.from_sql(f"""
            select {columns}
                 from files f, temp_files t
                 where t.id = f.id or (f.namespace = t.namespace and f.name = t.name)
        """)
        
    @staticmethod
    def get(db, fid = None, namespace = None, name = None, with_metadata = False):
        
        assert (fid is not None) != (namespace is not None or name is not None), "Can not specify both FID and namespace.name"
        assert (namespace is None) == (name is None)
        c = db.cursor()
        fetch_meta = "metadata" if with_metadata else "null"
        attrs = DBFile.attr_columns()
        if fid is not None:
            c.execute(f"""select id, namespace, name, {fetch_meta}, {attrs} 
                    from files
                    where id = %s""", (fid,))
        else:
            c.execute(f"""select id, namespace, name, {fetch_meta}, {attrs} 
                    from files
                    where namespace = %s and name=%s""", (namespace, name))
        tup = c.fetchone()
        return DBFile.from_tuple(db, tup)
        
    @staticmethod
    def exists(db, fid = None, namespace = None, name = None):
        #print("DBFile.exists:", fid, namespace, name)
        if fid is not None:
            assert (namespace is None) and (name is None),  "If FID is specified, namespace and name must be null"
        else:
            assert (namespace is not None) and (name is not None), "Both namespace and name must be specified"
        c = db.cursor()
        if fid is not None:
            c.execute("""select namespace, name 
                    from files
                    where id = %s""", (fid,))
        else:
            c.execute("""select id 
                    from files
                    where namespace = %s and name=%s""", (namespace, name))
        return c.fetchone() != None
        
    def fetch_metadata(self):
        c = self.DB.cursor()
        c.execute("""
            select metadata
                from files
                where id=%s""", (self.FID,))
        meta = None
        tup = c.fetchone()
        if tup is not None:
            meta = tup[0] or {}
        return meta
        
    def with_metadata(self):
        if not self.Metadata:
            self.Metadata = self.fetch_metadata()
        return self
    
    def metadata(self):
        if not self.Metadata:
            self.Metadata = self.fetch_metadata()
        return self.Metadata
        
    @staticmethod
    def list(db, namespace=None):
        c = db.cursor()
        if namespace is None:
            c.execute("""select id, namespace, name from files""")
        else:
            c.execute("""select id, namespace, name from files
                where namespace=%s""", (namespace,))
        return DBFileSet.from_tuples(db, fetch_generator(c))

    def has_attribute(self, attrname):
        return attrname in self.Metadata
        
    def get_attribute(self, attrname, default=None):
        return self.Metadata.get(attrname, default)

    def to_jsonable(self, with_datasets = False):
        ns = self.Name if self.Namespace is None else self.Namespace + ':' + self.Name
        data = dict(
            fid = self.FID,
            namespace = self.Namespace,
            name = ns
        )
        if self.Checksums is not None:  data["checksums"] = self.Checksums
        if self.Size is not None:       data["size"] = self.Size
        if self.Metadata is not None:   data["metadata"] = self.Metadata
        if self.Parents is not None:    data["parents"] = [p.FID if isinstance(p, DBFile) else p for p in self.Parents]
        if self.Children is not None:   data["children"] = [c.FID if isinstance(c, DBFile) else c for c in self.Children]
        if with_datasets:
            data["datasets"] = [{
                "namespace":ds.Namespace, "name":ds.Name
            } for ds in self.datasets()]
        return data

    def to_json(self, with_metadata = False, with_relations=False):
        return json.dumps(self.to_jsonable(with_metadata=with_metadata, with_relations=with_relations))
        
    def children(self, with_metadata = False):
        return DBFileSet(self.DB, [self]).children(with_metadata)
        
    def parents(self, with_metadata = False):
        return DBFileSet(self.DB, [self]).parents(with_metadata)
        
    def add_child(self, child, do_commit=True):
        child_fid = child if isinstance(child, str) else child.FID
        c = self.DB.cursor()
        c.execute("""
            insert into parent_child(parent_id, child_id)
                values(%s, %s)        
                on conflict(parent_id, child_id) do nothing;
            """, (self.FID, child_fid)
        )
        if do_commit:   c.execute("commit")
        
    def add_parents(self, parents, do_commit=True):
        parent_fids = [(p if isinstance(p, str) else p.FID,) for p in parents]
        c = self.DB.cursor()
        c.executemany(f"""
            insert into parent_child(parent_id, child_id)
                values(%s, '{self.FID}')        
                on conflict(parent_id, child_id) do nothing;
            """, parent_fids
        )
        if do_commit:   c.execute("commit")
        
    def set_parents(self, parents, do_commit=True):
        parent_fids = [(p if isinstance(p, str) else p.FID,) for p in parents]
        c = self.DB.cursor()
        #print("set_parents: fids:", parent_fids)
        c.execute(f"delete from parent_child where child_id='{self.FID}'")
        c.executemany(f"""
            insert into parent_child(parent_id, child_id)
                values(%s, '{self.FID}')        
                on conflict(parent_id, child_id) do nothing;
            """, parent_fids
        )
        if do_commit:   c.execute("commit")
        
    def remove_child(self, child, do_commit=True):
        child_fid = child if isinstance(child, str) else child.FID
        c = self.DB.cursor()
        c.execute("""
            delete from parent_child where
                parent_id = %s and child_id = %s;
            """, (self.FID, child_fid)
        )
        if do_commit:   c.execute("commit")

    def add_parent(self, parent, do_commit=True):
        parent_fid = parent if isinstance(parent, str) else parent.FID
        return DBFile(self.DB, fid=parent_fid).add_child(self, do_commit=do_commit)
        
    def remove_parent(self, parent, do_commit=True):
        parent_fid = parent if isinstance(parent, str) else parent.FID
        return DBFile(self.DB, fid=parent_fid).remove_child(self, do_commit=do_commit)
        
    def datasets(self):
        # list all datasets this file is found in
        c = self.DB.cursor()
        c.execute("""
            select fds.dataset_namespace, fds.dataset_name
                from files_datasets fds
                where fds.file_id = %s
                order by fds.dataset_namespace, fds.dataset_name""", (self.FID,))
        return (DBDataset(self.DB, namespace, name) for namespace, name in fetch_generator(c))
        
class MetaExpressionDNF(object):
    
    def __init__(self, exp):
        #
        # meta_exp is a nested list representing the query filter expression in DNF:
        #
        # meta_exp = [meta_or, ...]
        # meta_or = [meta_and, ...]
        # meta_and = [(op, aname, avalue), ...]
        #
        debug("===MetaExpressionDNF===")
        self.Exp = None
        self.DNF = None
        if exp is not None:
            #
            # converts canonic Node expression (meta_or of one or more meta_ands) into nested or-list or and-lists
            #
            #assert isinstance(self.Exp, Node)
            assert exp.T == "meta_or"
            for c in exp.C:
                assert c.T == "meta_and"
    
            or_list = []
            for and_item in exp.C:
                or_list.append(and_item.C)
            self.DNF = or_list

        #print("MetaExpressionDNF: exp:", self.DNF)
        #self.validate_exp(meta_exp)
        
    def __str__(self):
        return self.file_ids_sql()
        
    __repr__= __str__
    
    def sql_and(self, and_term, table_name):
        

        def sql_literal(v):
            if isinstance(v, str):       v = "'%s'" % (v,)
            elif isinstance(v, bool):    v = "true" if v else "false"
            elif v is None:              v = "null"
            else:   v = str(v)
            return v
            
        def json_literal(v):
            if isinstance(v, str):       v = '"%s"' % (v,)
            else:   v = sql_literal(v)
            return v
            
        def pg_type(v):
            if isinstance(v, bool):   pgtype='boolean'
            elif isinstance(v, str):   pgtype='text'
            elif isinstance(v, int):   pgtype='bigint'
            elif isinstance(v, float):   pgtype='double precision'
            else:
                raise ValueError("Unrecognized literal type: %s %s" % (v, type(v)))
            return pgtype
            
        contains_items = []
        parts = []
        
        for exp in and_term:
            debug("sql_and:")
            debug(exp.pretty("    "))
            
            op = exp.T
            args = exp.C
            negate = False

            term = ""

            if op == "present":
                aname = exp["name"]
                if not '.' in aname:
                    term = "true" if aname in DBFile.ColumnAttributes else "false"
                else:
                    term = f"{table_name}.metadata ? '{aname}'"

            elif op == "not_present":
                aname = exp["name"]
                if not '.' in aname:
                    term = "false" if aname in DBFile.ColumnAttributes else "true"
                else:
                    term = f"{table_name}.metadata ? '{aname}'"
            
            else:
                assert op in ("cmp_op", "in_range", "in_set", "not_in_range", "not_in_set")
                arg = args[0]
                assert arg.T in ("array_any", "array_subscript","array_length","scalar")
                negate = exp["neg"]
                aname = arg["name"]
                if not '.' in aname:
                    assert arg.T == "scalar"
                    assert aname in DBFile.ColumnAttributes
                    
                if arg.T == "array_subscript":
                    # a[i] = x
                    aname, inx = arg["name"], arg["index"]
                    inx = json_literal(inx)
                    subscript = f"[{inx}]"
                elif arg.T == "array_any":
                    aname = arg["name"]
                    subscript = "[*]"
                elif arg.T == "scalar":
                    aname = arg["name"]
                    subscript = ""
                elif arg.T == "array_length":
                    aname = arg["name"]
                else:
                    raise ValueError(f"Unrecognozed argument type={arg.T}")

                #parts.append(f"{table_name}.metadata ? '{aname}'")

                    
                # - query time slows down significantly if this is addded
                #if arg.T in ("array_subscript", "array_any", "array_all"):
                #    # require that "aname" is an array, not just a scalar
                #    parts.append(f"{table_name}.metadata @> '{{\"{aname}\":[]}}'")
                
                if op == "in_range":
                    assert len(args) == 1
                    typ, low, high = exp["type"], exp["low"], exp["high"]
                    low = json_literal(low)
                    high = json_literal(high)
                    if not '.' in aname:
                        low = sql_literal(low)
                        high = sql_literal(high)
                        term = f"{table_name}.{aname} between {low} and {high}"
                    elif arg.T in ("array_subscript", "scalar", "array_any"):
                        term = f"{table_name}.metadata @? '$.\"{aname}\"{subscript} ? (@ >= {low} && @ <= {high})'"
                    elif arg.T == "array_length":
                        n = "not" if negate else ""
                        negate = False
                        term = f"jsonb_array_length({table_name}.metadata -> '{aname}') {n} between {low} and {high}"
                        
                if op == "not_in_range":
                    assert len(args) == 1
                    typ, low, high = exp["type"], exp["low"], exp["high"]
                    low = json_literal(low)
                    high = json_literal(high)
                    if not '.' in aname:
                        low = sql_literal(low)
                        high = sql_literal(high)
                        term = f"not ({table_name}.{aname} between {low} and {high})"
                    elif arg.T in ("array_subscript", "scalar", "array_any"):
                        term = f"{table_name}.metadata @? '$.\"{aname}\"{subscript} ? (@ < {low} || @ > {high})'"
                    elif arg.T == "array_length":
                        n = "" if negate else "not"
                        negate = False
                        term = f"jsonb_array_length({table_name}.metadata -> '{aname}') {n} between {low} and {high}"
                        
                elif op == "in_set":
                    if not '.' in aname:
                        values = [sql_literal(v) for v in exp["set"]]
                        value_list = ",".join(values)
                        term = f"{table_name}.{aname} in ({value_list})"
                    elif arg.T == "array_length":
                        values = [sql_literal(v) for v in exp["set"]]
                        value_list = ",".join(values)
                        n = "not" if negate else ""
                        negate = False
                        term = f"jsonb_array_length({table_name}.metadata -> '{aname}') {n} in ({value_list})"
                    else:           # arg.T in ("array_any", "array_subscript","scalar")
                        values = [json_literal(x) for x in exp["set"]]
                        or_parts = [f"@ == {v}" for v in values]
                        predicate = " || ".join(or_parts)
                        term = f"{table_name}.metadata @? '$.\"{aname}\"{subscript} ? ({predicate})'"
                        
                elif op == "not_in_set":
                    if not '.' in aname:
                        values = [sql_literal(v) for v in exp["set"]]
                        value_list = ",".join(values)
                        term = f"not ({table_name}.{aname} in ({value_list}))"
                    elif arg.T == "array_length":
                        values = [sql_literal(v) for v in exp["set"]]
                        value_list = ",".join(values)
                        n = "" if negate else "not"
                        negate = False
                        term = f"not(jsonb_array_length({table_name}.metadata -> '{aname}') {n} in ({value_list}))"
                    else:           # arg.T in ("array_any", "array_subscript","scalar")
                        values = [json_literal(x) for x in exp["set"]]
                        and_parts = [f"@ != {v}" for v in values]
                        predicate = " && ".join(and_parts)
                        term = f"{table_name}.metadata @? '$.\"{aname}\"{subscript} ? ({predicate})'"
                        
                elif op == "cmp_op":
                    cmp_op = exp["op"]
                    if cmp_op == '=': cmp_op = "=="
                    sql_cmp_op = "=" if cmp_op == "==" else cmp_op
                    value = args[1]
                    value_type, value = value.T, value["value"]
                    sql_value = sql_literal(value)
                    value = json_literal(value)
                    
                    if not '.' in aname:
                        term = f"{table_name}.{aname} {sql_cmp_op} {sql_value}"
                    elif arg.T == "array_length":
                        term = f"jsonb_array_length({table_name}.metadata -> '{aname}') {sql_cmp_op} {value}"
                    else:
                        if cmp_op in ("~", "~*", "!~", "!~*"):
                            negate_predicate = False
                            if cmp_op.startswith('!'):
                                cmp_op = cmp_op[1:]
                                negate_predicate = not negate_predicate
                            flags = ' flag "i"' if cmp_op.endswith("*") else ''
                            cmp_op = "like_regex"
                            value = f"{value}{flags}"
                        
                            predicate = f"@ like_regex {value} {flags}"
                            if negate_predicate: 
                                predicate = f"!({predicate})"
                            term = f"{table_name}.metadata @? '$.\"{aname}\"{subscript} ? ({predicate})'"

                        else:
                            # scalar, array_subscript, array_any
                            term = f"{table_name}.metadata @@ '$.\"{aname}\"{subscript} {cmp_op} {value}'"
                    
            if negate:  term = f"not ({term})"
            parts.append(term)

        if contains_items:
            parts.append("%s.metadata @> '{%s}'" % (table_name, ",".join(contains_items )))
            
        if Debug:
            print("sql_and():")
            print(" and_terms:")
            for t in and_term:
                print(t.pretty("    "))
            print("output parts:")
            for p in parts:
                print("      ", p)
            
        return " and ".join([f"({p})" for p in parts])
        
    def sql(self, table_name):
        if self.DNF:
            return " or ".join([self.sql_and(t, table_name) for t in self.DNF])
        else:
            return None
            
class DBDataset(object):

    def __init__(self, db, namespace, name, parent_namespace=None, parent_name=None, frozen=False, monotonic=False, metadata={}):
        assert namespace is not None and name is not None
        assert (parent_namespace is None) == (parent_name == None)
        self.DB = db
        self.Namespace = namespace
        self.Name = name
        self.ParentNamespace = parent_namespace
        self.ParentName = parent_name
        self.SQL = None
        self.Frozen = frozen
        self.Monotonic = monotonic
        self.Creator = None
        self.CreatedTimestamp = None
        self.Metadata = metadata
        self.Description = None
    
    def __str__(self):
        return "DBDataset(%s:%s)" % (self.Namespace, self.Name)
        
    def save(self, do_commit = True):
        c = self.DB.cursor()
        namespace = self.Namespace.Name if isinstance(self.Namespace, DBNamespace) else self.Namespace
        parent_namespace = self.ParentNamespace.Name if isinstance(self.ParentNamespace, DBNamespace) else self.ParentNamespace
        meta = json.dumps(self.Metadata or {})
        #print("DBDataset.save: saving")
        c.execute("""
            insert into datasets(namespace, name, parent_namespace, parent_name, frozen, monotonic, metadata, creator, created_timestamp,
                        description) 
                values(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict(namespace, name) 
                    do update set parent_namespace=%s, parent_name=%s, frozen=%s, monotonic=%s, metadata=%s, description=%s
            """,
            (namespace, self.Name, parent_namespace, self.ParentName, self.Frozen, self.Monotonic, meta, self.Creator, self.CreatedTimestamp,
                    self.Description, 
                    parent_namespace, self.ParentName, self.Frozen, self.Monotonic, meta, self.Description
            )
        )
        if do_commit:   c.execute("commit")
        return self
            
    def add_file(self, f, do_commit = True):
        assert isinstance(f, DBFile)
        c = self.DB.cursor()
        c.execute("""
            insert into files_datasets(file_id, dataset_namespace, dataset_name) values(%s, %s, %s)
                on conflict do nothing""",
            (f.FID, self.Namespace, self.Name))
        if do_commit:   c.execute("commit")
        return self
        
    def add_files(self, files, do_commit=True):
        c = self.DB.cursor()
        c.executemany(f"""
            insert into files_datasets(file_id, dataset_namespace, dataset_name) values(%s, '{self.Namespace}', '{self.Name}')
                on conflict do nothing""", ((f.FID,) for f in files))
        if do_commit:
            c.execute("commit")
        return self
        
    def list_files(self, with_metadata=False, limit=None):
        meta = "null as metadata" if not with_metadata else "f.metadata"
        limit = f"limit {limit}" if limit else ""
        sql = f"""select f.id, f.namespace, f.name, {meta}, f.size, f.checksums, f.creator, f.created_timestamp 
                    from files f
                        inner join files_datasets fd on fd.file_id = f.id
                    where fd.dataset_namespace = %s and fd.dataset_name=%s
                    {limit}
        """
        c = self.DB.cursor()
        c.execute(sql, (self.Namespace, self.Name))
        for fid, namespace, name, meta, size, checksums, creator, created_timestamp in fetch_generator(c):
            meta = meta or {}
            checksums = checksums or {}
            f = DBFile(self.DB, fid=fid, namespace=namespace, name=name, metadata=meta, size=size, checksums = checksums)
            f.Creator = creator
            f.CreatedTimestamp = created_timestamp
            yield f
        
        
        
    @staticmethod
    def get(db, namespace, name):
        c = db.cursor()
        namespace = namespace.Name if isinstance(namespace, DBNamespace) else namespace
        #print(namespace, name)
        c.execute("""select parent_namespace, parent_name, frozen, monotonic, metadata, creator, created_timestamp, description
                        from datasets
                        where namespace=%s and name=%s""",
                (namespace, name))
        tup = c.fetchone()
        if tup is None: return None
        parent_namespace, parent_name, frozen, monotonic, meta, creator, created_timestamp, description = tup
        dataset = DBDataset(db, namespace, name, parent_namespace, parent_name)
        dataset.Frozen = frozen
        dataset.Monotonic = monotonic
        dataset.Metadata = meta or {}
        dataset.Creator = creator
        dataset.CreatedTimestamp = created_timestamp
        return dataset

    @staticmethod
    def exists(db, namespace, name):
        return DBDataset.get(db, namespace, name) is not None

    @staticmethod
    def list(db, namespace=None, parent_namespace=None, parent_name=None, creator=None):
        namespace = namespace.Name if isinstance(namespace, DBNamespace) else namespace
        parent_namespace = parent_namespace.Name if isinstance(parent_namespace, DBNamespace) else parent_namespace
        creator = creator.Username if isinstance(creator, DBUser) else creator
        wheres = []
        if namespace is not None:
            wheres.append("namespace = '%s'" % (namespace,))
        if parent_namespace is not None:
            wheres.append("parent_namespace = '%s'" % (parent_namespace,))
        if parent_name is not None:
            wheres.append("parent_name = '%s'" % (parent_name,))
        if creator is not None:
            wheres.append("creator = '%s'" % (creator,))
        wheres = "" if not wheres else "where " + " and ".join(wheres)
        c=db.cursor()
        c.execute("""select namespace, name, parent_namespace, parent_name, frozen, monotonic, metadata,
                            creator, created_timestamp
                from datasets %s""" % (wheres,))
        for namespace, name, parent_namespace, parent_name, frozen, monotonic, meta, creator, created_timestamp in fetch_generator(c):
            ds = DBDataset(db, namespace, name, parent_namespace, parent_name, frozen, monotonic, metadata=meta)
            ds.Creator = creator
            ds.CreatedTimestamp = created_timestamp
            yield ds

    @property
    def nfiles(self):
        c = self.DB.cursor()
        c.execute("""select count(*) 
                        from files_datasets 
                        where dataset_namespace=%s and dataset_name=%s""", (self.Namespace, self.Name))
        return c.fetchone()[0]     
    
    def to_jsonable(self):
        return dict(
            namespace = self.Namespace.Name if isinstance(self.Namespace, DBNamespace) else self.Namespace,
            name = self.Name,
            parent_namespace = self.ParentNamespace.Name if isinstance(self.ParentNamespace, DBNamespace) else self.ParentNamespace,
            parent_name = self.ParentName,
            metadata = self.Metadata or {},
            creator = self.Creator,
            created_timestamp = epoch(self.CreatedTimestamp)
        )
    
    def to_json(self):
        return json.dumps(self.to_jsonable())
        
    @staticmethod
    def list_datasets(db, patterns, with_children, recursively, limit=None):
        #
        # does not use "having" yet !
        #
        datasets = set()
        c = db.cursor()
        #print("DBDataset.list_datasets: patterns:", patterns)
        for pattern in patterns:
            match = pattern["wildcard"]
            namespace = pattern["namespace"]
            name = pattern["name"]
            #print("list_datasets: match, namespace, name:", match, namespace, name)
            if match:
                sql = """select namespace, name, metadata from datasets
                            where namespace = '%s' and name like '%s'""" % (namespace, name)
                #print("list_datasets: sql:", sql)
                c.execute(sql)
            else:
                c.execute("""select namespace, name, metadata from datasets
                            where namespace = %s and name = %s""", (namespace, name))
            for namespace, name, meta in c.fetchall():
                #print("list_datasets: add", namespace, name)
                datasets.add((namespace, name))
                
        #print("list_datasets: with_children:", with_children)
        if with_children:
            parents = datasets.copy()
            children = set()
            parents_scanned = set()
            while parents:
                this_level_children = set()
                for pns, pn in parents:
                    c.execute("""select namespace, name from datasets
                                where parent_namespace = %s and parent_name=%s""",
                                (pns, pn))
                    for ns, n in c.fetchall():
                        this_level_children.add((ns, n))
                parents_scanned |= parents
                datasets |= this_level_children
                if recursively:
                    parents = this_level_children - parents_scanned
                else:
                    parents = set()
        return limited((DBDataset.get(db, namespace, name) for namespace, name in datasets), limit)

    @staticmethod    
    def apply_dataset_selector(db, dataset_selector, limit):
        patterns = dataset_selector.Patterns
        with_children = dataset_selector.WithChildren
        recursively = dataset_selector.Recursively
        datasets = DBDataset.list_datasets(db, patterns, with_children, recursively)
        return limited(dataset_selector.filter_by_having(datasets), limit)


    """
        Recursive query:
        
        with recursive subs as (
                select manager_id, employee_id, full_name
                        from employees
                        where true
                union
                        select s.manager_id, e.employee_id, e.full_name
                        from employees e
                                inner join subs s on s.employee_id = e.manager_id
        )
        select * from subs
        ;
        
        
        
        """

    @staticmethod   
    def sql_for_selector(selector):
        meta_where_clause = ""
        ds_alias = alias("ds")
        meta = "null as metadata"
        if selector.Having is not None:
            meta_where_clause = "where " + MetaExpressionDNF(selector.Having).sql(ds_alias)            
            meta = "metadata"
        parts = []
        for p in selector.Patterns:
            namespace = p["namespace"]
            name_pattern = p["name"]
            wildcard = p["wildcard"]
            
            if wildcard:
                base_query = f"""
                        select namespace, name, {meta} from datasets where namespace='{namespace}' and name like '{name_pattern}'
                    """
            elif meta_where_clause:
                base_query = f"""
                                    select namespace, name, {meta} from datasets where namespace='{namespace}' and name='{name_pattern}'
                                """
            else:
                base_query = f"select '{namespace}' as namespace, '{name_pattern}' as name, null as metadata"
            
            parts.append(base_query)

            if selector.WithChildren:
                ds = alias("ds")
                d = alias("ds")
                s = alias("s")
                if selector.Recursively:
                    sql = f"""
                        (
                            with recursive subsets as (
                                select {ds}.namespace, {ds}.name, {ds}.metadata 
                                from datasets {ds} 
                                where {ds}.parent_namespace='{namespace}' and {ds}.parent_name like '{name_pattern}'
                                union
                                    select {d}.namespace, {d}.name, {d}.metadata from datasets {d}
                                        inner join subsets {s} on {s}.namespace = {d}.parent_namespace and {s}.name = {d}.parent_name
                            )
                            select distinct * from subsets
                        )"""
                else:
                    sql = f"""
                    select {ds}.namespace, {ds}.name, {ds}.metadata 
                    from datasets {ds} 
                    where {ds}.parent_namespace='{namespace}' and {ds}.parent_name like '{name_pattern}'
                    """
                parts.append(sql)

        sql = "\nunion\n".join(parts)
        if meta_where_clause:
            sql = f"select namespace, name from ({sql}) as {ds_alias} {meta_where_clause}"

        return sql
        
class DBNamedQuery(object):

    def __init__(self, db, namespace, name, source, parameters=[]):
        assert namespace is not None and name is not None
        self.DB = db
        self.Namespace = namespace
        self.Name = name
        self.Source = source
        self.Parameters = parameters
        self.Creator = None
        self.CreatedTimestamp = None
        
    def save(self):
        self.DB.cursor().execute("""
            insert into queries(namespace, name, source, parameters) values(%s, %s, %s, %s)
                on conflict(namespace, name) 
                    do update set source=%s, parameters=%s;
            commit""",
            (self.Namespace, self.Name, self.Source, self.Parameters, self.Source, self.Parameters))
        return self
            
    @staticmethod
    def get(db, namespace, name):
        c = db.cursor()
        debug("DBNamedQuery:get():", namespace, name)
        c.execute("""select source, parameters
                        from queries
                        where namespace=%s and name=%s""",
                (namespace, name))
        (source, params) = c.fetchone()
        return DBNamedQuery(db, namespace, name, source, params)
        
    @staticmethod
    def list(db, namespace=None):
        c = db.cursor()
        if namespace is not None:
            c.execute("""select namespace, name, source, parameters
                        from queries
                        where namespace=%s""",
                (namespace,)
            )
        else:
            c.execute("""select namespace, name, source, parameters
                        from queries"""
            )
        return (DBNamedQuery(db, namespace, name, source, parameters) 
                    for namespace, name, source, parameters in fetch_generator(c)
        )

class Authenticator(object):
    
    def __init__(self, username, secrets=[]):
        self.Username = username
        self.Secrets = secrets[:]
    
    @staticmethod
    def from_db(username, typ, secrets):
        if typ == "password":   a = PasswordAuthenticator(username, secrets)
        elif typ == "x509":   a = X509Authenticator(username, secrets)
        else:
            raise ValueError(f"Unknown autenticator type {typ}")
        return a
    
    def addSecret(self, new_secret):
        raise NotImplementedError
        
    def setSecret(self, secret):
        self.Secrets = [secret]
        
    def verifySecret(self, secret):
        raise NotImplementedError
        
class PasswordAuthenticator(Authenticator):
    
    def addSecret(self,new_secret):
        raise NotImplementedError("Can not add secret to a password authenticator. Use setSecret() instead")
        
    def format_secret(self, hashed_password):
        if hashed_password.startswith("$") and ":" in hashed_password: return hashed_password
        return f"${PasswordHashAlgorithm}:{hashed_password}"
        
    def unpack_password(self, secret):
        if secret.startswith("$") and ":" in secret:
            secret_alg, password = secret[1:].split(":", 1)
        else:
            password = secret
        return password
        
    def hashed_password(self):
        return self.unpack_password(self.Secrets[0])
        
    def setSecret(self, hashed_password):
        # should never be used by the Server because Server will always see only hashed password!
        self.Secrets = [self.format_secret(hashed_password)]

    def verifySecret(self, hashed_password):
        # password is supposed to be hashed password
        return self.hashed_password() == hashed_password
            
class X509Authenticator(Authenticator):
    
    HashAlg = "sha1"
    
    def addSecret(self, dn):
        if not new_secret in self.Secrets:
            self.Secrets.append(dn)

    def setSecret(self, dn):
        self.Secrets = [dn]

    def verifySecret(self, dn):
        return dn in self.Secrets
        
class _DBManyToMany(object):
    
    def __init__(self, db, table, *variable, **fixed):
        self.DB = db
        self.Table = table
        assert len(fixed) == 1
        self.FixedColumn, self.FixedValue = list(fixed.items())[0]
        self.Where = "where %s = '%s'" % (self.FixedColumn, self.FixedValue)
        assert len(variable) >= 1
        self.VarColumns = list(variable)
        
    def list(self, c=None):
        columns = ",".join(self.VarColumns) 
        if c is None: c = self.DB.cursor()
        c.execute(f"select {columns} from {self.Table} {self.Where}")
        if len(self.VarColumns) == 1:
            return (x for (x,) in fetch_generator(c))
        else:
            return fetch_generator(c)
        
    def __iter__(self):
        return self.list()
        
    def add(self, *vals, c=None):
        assert len(vals) == len(self.VarColumns)
        col_vals = list(zip(self.VarColumns, vals)) + [(self.FixedColumn, self.FixedValue)]
        cols, vals = zip(*col_vals)
        cols = ",".join(cols)
        vals = ",".join([f"'{v}'" for v in vals])
        if c is None: c = self.DB.cursor()
        c.execute(f"""
            insert into {self.Table}({cols}) values({vals})
                on conflict({cols}) do nothing
        """)
        return self
        
    def contains(self, *vals, c=None):
        assert len(vals) == len(self.VarColumns)
        col_vals = list(zip(self.VarColumns, vals))
        where = self.Where + " and " + " and ".join(["%s='%s'" % (k,v) for k, v in col_vals])
        if c is None: c = self.DB.cursor()
        c.execute(f"select {self.FixedColumn} from {self.Table} {where}")
        return c.fetchone() is not None
        
    def __contains__(self, v):
        if not isinstance(v, tuple): v = (v,)
        return self.contains(*v)

    def remove(self, *vals, c=None, all=False):
        assert all or len(vals) == len(self.VarColumns)
        if c is None: c = self.DB.cursor()
        where = self.Where
        if not all:
            col_vals = list(zip(self.VarColumns, vals))
            where += " and " + " and ".join(["%s='%s'" % (k,v) for k, v in col_vals])
        c.execute(f"delete from {self.Table} {where}")
        return self
        
    def set(self, lst, c=None):
        if c is None: c = self.DB.cursor()
        c.execute("begin")
        self.remove(all=True, c=c)
        for tup in lst:
            if not isinstance(tup, tuple):  tup = (tup,)
            self.add(*tup, c=c)
        c.execute("commit")
        
class DBUser(object):

    def __init__(self, db, username, name, email, flags=""):
        self.Username = username
        self.Name = name
        self.EMail = email
        self.Flags = flags
        self.DB = db
        self.Authenticators = {}        # type -> [secret,...]
        self.RoleNames = None
        
    def __str__(self):
        return "DBUser(%s, %s, %s, %s)" % (self.Username, self.Name, self.EMail, self.Flags)
        
    __repr__ = __str__
    
    def save(self, do_commit=True):
        c = self.DB.cursor()
        c.execute("""
            insert into users(username, name, email, flags) values(%s, %s, %s, %s)
                on conflict(username) 
                    do update set name=%s, email=%s, flags=%s;
            """,
            (self.Username, self.Name, self.EMail, self.Flags, self.Name, self.EMail, self.Flags))
        
        c.execute("delete from authenticators where username=%s", (self.Username,))
        c.executemany("insert into authenticators(username, type, secrets) values(%s, %s, %s)",
            [(self.Username, typ, a.Secrets) for typ, a in self.Authenticators.items()])

        if do_commit:
            c.execute("commit")
        return self
        
    def set_password(self, password):
        a = self.Authenticators.setdefault("password", PasswordAuthenticator(self.Username))
        a.setSecret(password)
        
    def verify_password(self, password):
        a = self.Authenticators.get("password")
        if not a:
            return False, "No password found"
        if not a.verifySecret(password):
            return False, "Password mismatch"
        return True, "OK"

    @staticmethod
    def get(db, username):
        c = db.cursor()
        c.execute("""select u.name, u.email, u.flags, array(select ur.role_name from users_roles ur where ur.username=u.username)
                        from users u
                        where u.username=%s""",
                (username,))
        tup = c.fetchone()
        if not tup: return None
        (name, email, flags, roles) = tup
        u = DBUser(db, username, name, email, flags)
        c.execute("""select type, secrets from authenticators where username=%s""", (username,))
        u.Authenticators = {typ:Authenticator.from_db(username, typ, secrets) for typ, secrets in c.fetchall()}
        u.RoleNames = roles
        return u
        
    def is_admin(self):
        return "a" in (self.Flags or "")
    
    @staticmethod 
    def list(db):
        c = db.cursor()
        c.execute("""select u.username, u.name, u.email, u.flags, array(select ur.role_name from users_roles ur where ur.username=u.username)
            from users u
        """)
        for username, name, email, flags, roles in c.fetchall():
            u = DBUser(db, username, name, email, flags)
            u.RoleNames = roles
            #print("DBUser.list: yielding:", u)
            yield u
            
    @property
    def roles(self):
        return _DBManyToMany(self.DB, "users_roles", "role_name", username = self.Username)
        
    def namespaces(self):
        return DBNamespace.list(self.DB, owned_by_user=self)        
        
    def add_role(self, role):
        self.roles.add(role.Name if isinstance(role, DBRole) else role)

    def remove_role(self, role):
        self.roles.remove(role.Name if isinstance(role, DBRole) else role)

class DBNamespace(object):

    def __init__(self, db, name, owner_user=None, owner_role=None, description=None):
        self.Name = name
        assert None in (owner_user, owner_role)
        self.OwnerUser = owner_user
        self.OwnerRole = owner_role
        self.Description = description
        self.DB = db
        self.Creator = None
        self.CreatedTimestamp = None
        
    def to_jsonable(self):
        return dict(
            name=self.Name,
            owner_user=self.OwnerUser,
            owner_role=self.OwnerRole,
            creator = self.Creator,
            description = self.Description,
            created_timestamp = epoch(self.CreatedTimestamp)
        )
        
    def save(self, do_commit=True):
        c = self.DB.cursor()
        c.execute("""
            insert into namespaces(name, owner_user, owner_role, description, creator) values(%s, %s, %s, %s, %s)
                on conflict(name) 
                    do update set owner_user=%s, owner_role=%s, description=%s, creator=%s;
            commit
            """,
            (self.Name, self.OwnerUser, self.OwnerRole, self.Description, self.Creator, self.OwnerUser, self.OwnerRole, self.Description, self.Creator))
        if do_commit:
            c.execute("commit")
        return self

    def create(self, do_commit=True):
        c = self.DB.cursor()
        c.execute("""
            insert into namespaces(name, owner_user, owner_role, description, creator) values(%s, %s, %s, %s, %s)
            """,
            (self.Name, self.OwnerUser, self.OwnerRole, self.Description, self.Creator))
        if do_commit:
            c.execute("commit")
        return self
        
    @staticmethod
    def get(db, name):
        #print("DBNamespace.get: name:", name)
        c = db.cursor()
        c.execute("""select owner_user, owner_role, description, creator, created_timestamp 
                from namespaces where name=%s""", (name,))
        tup = c.fetchone()
        if not tup: return None
        owner_user, owner_role, description, creator, created_timestamp = tup
        ns = DBNamespace(db, name, owner_user, owner_role, description)
        ns.Creator = creator
        ns.CreatedTimestamp = created_timestamp
        return ns
        
    @staticmethod
    def exists(db, name):
        return DBNamespace.get(db, name) != None
        
    @staticmethod
    def list(db, owned_by_user=None, owned_by_role=None, directly=False):
        c = db.cursor()
        if isinstance(owned_by_user, DBUser):   owned_by_user = owned_by_user.Username
        if isinstance(owned_by_role, DBRole):   owned_by_role = owned_by_role.Name
        if owned_by_user is not None:
            sql = """
                select name, owner_user, owner_role, description, creator, created_timestamp 
                        from namespaces
                        where owner_user=%s
            """
            args = (owned_by_user,)
            if not directly:
                sql += """
                    union
                    select name, owner_user, owner_role, description, creator, created_timestamp 
                            from namespaces ns, users_roles ur
                            where ur.username = %s and ur.role_name = ns.owner_role
                """
                args = args + (owned_by_user,)
        elif owned_by_role is not None:
            sql = """select name, owner_user, owner_role, description, creator, created_timestamp 
                        from namespaces
                        where owner_role=%s
            """
            args = (owned_by_role,)
        else:
            sql = """select name, owner_user, owner_role, description, creator, created_timestamp 
                        from namespaces
            """
            args = ()
        #print("DBNamespace.list: sql, args:", sql, args)
        c.execute(sql, args)
        for name, owner_user, owner_role, description, creator, created_timestamp in c.fetchall():
            ns = DBNamespace(db, name, owner_user, owner_role, description)
            ns.Creator = creator
            ns.CreatedTimestamp = created_timestamp
            yield ns

    def owners(self, directly=False):
        if self.OwnerUser is not None:
            return [self.OwnerUser]
        elif not directly and self.OwnerRole is not None:
            r = self.OwnerRole
            if isinstance(r, str):
                r = DBRole(self.DB, r)
            return r.members
        else:
            return []

    def owned_by_user(self, user, directly=False):
        if isinstance(user, DBUser):   user = user.Username
        return user in self.owners(directly)
        
    def owned_by_role(self, role):
        if isinstance(role, DBRole):   role = role.name
        return self.OwnerRole == role

    def file_count(self):
        c = self.DB.cursor()
        c.execute("""select count(*) from files where namespace=%s""", (self.Name,))
        tup = c.fetchone()
        if not tup: return 0
        else:       return tup[0]
        
    def dataset_count(self):
        c = self.DB.cursor()
        c.execute("""select count(*) from datasets where namespace=%s""", (self.Name,))
        tup = c.fetchone()
        if not tup: return 0
        else:       return tup[0]
        
    def query_count(self):
        c = self.DB.cursor()
        c.execute("""select count(*) from queries where namespace=%s""", (self.Name,))
        tup = c.fetchone()
        if not tup: return 0
        else:       return tup[0]

class DBRole(object):

    def __init__(self, db, name, description=None, users=[]):
        self.Name = name
        self.Description = description
        self.DB = db
            
    def __str__(self):
        return "[DBRole %s %s]" % (self.Name, self.Description)
        
    __repr__ = __str__

    @property
    def members(self):
        return _DBManyToMany(self.DB, "users_roles", "username", role_name=self.Name)
        
    def save(self, do_commit=True):
        c = self.DB.cursor()
        c.execute("""
            insert into roles(name, description) values(%s, %s)
                on conflict(name) 
                    do update set description=%s
            """,
            (self.Name, self.Description, self.Description))
        if do_commit:   c.execute("commit")
        return self
        
    @staticmethod
    def get(db, name):
        c = db.cursor()
        c.execute("""select r.description
                        from roles r
                        where r.name=%s
        """, (name,))
        tup = c.fetchone()
        if not tup: return None
        (desc,) = tup
        return DBRole(db, name, desc)
        
    @staticmethod 
    def list(db, user=None):
        c = db.cursor()
        if isinstance(user, DBUser):    user = user.Username
        if user:
            c.execute("""select r.name, r.description
                        from roles r
                            inner join users_roles ur on ur.role_name=r.name
                    where ur.username = %s
                    order by r.name
            """, (user,))
        else:
            c.execute("""select r.name, r.description
                            from roles r
                            order by r.name""")
        
        out = [DBRole(db, name, description) for  name, description in fetch_generator(c)]
        #print("DBRole.list:", out)
        return out
        
    def add_member(self, user):
        self.members.add(user)
        return self
        
    def remove_member(self, user):
        self.members.remove(user)
        return self
        
    def set_members(self, users):
        self.members.set(users)
        return self
        
    def __contains__(self, user):
        if isinstance(user, DBUser):
            user = user.Username
        return user in self.members
        
    def __iter__(self):
        return self.members.__iter__()
            
class DBParamDefinition(object):
    
    Types =  ('int','double','text','boolean',
                'int[]','double[]','text[]','boolean[]')

    def __init__(self, db, name, typ, int_values = None, int_min = None, int_max = None, 
                            double_values = None, double_min = None, double_max = None,
                            text_values = None, text_pattern = None):
        self.DB = db
        self.Name = name
        self.Type = typ
        self.IntValues = int_values
        self.IntMin = int_min
        self.IntMax = int_max
        self.DoubleValues = double_values
        self.DoubleMin = double_min
        self.DoubleMax = double_max
        self.TextValues = text_values if text_pattern is None else set(text_values)
        self.TextPattern = text_pattern if text_pattern is None else re.compile(text_pattern)
        
        # TODO: add booleans, add is_null
        
    def as_jsonable(self):
        dct = dict(name = self.Name, type=self.Type)
        if self.Type in ("int", "int[]"):
            if self.IntMin is not None: dct["int_min"] = self.IntMin
            if self.IntMax is not None: dct["int_max"] = self.IntMax
            if self.IntValues: dct["int_values"] = self.IntValues
        elif self.Type in ("float", "float[]"):
            if self.IntMin is not None: dct["float_min"] = self.FloatMin
            if self.IntMax is not None: dct["float_max"] = self.FloatMax
            if self.IntValues: dct["float_values"] = self.FloatValues
        elif self.Type in ("text", "text[]"):
            if self.TextValues: dct["text_values"] = self.TextValues
            if self.TextPattern: dct["text_pattern"] = self.TextPattern
        return dct
        
    def as_json(self):
        return json.dumps(self.as_jsonable())
            
    @staticmethod
    def from_json(db, x):
        if isinstance(x, str):
            x = json.loads(x)
        d = DBParamDefinition(db, x["name"], x["type"],
            int_values = x.get("int_values"), int_min=x.get("int_min"), int_max=x.get("int_max"),
            float_values = x.get("float_values"), float_min=x.get("float_min"), float_max=x.get("float_max"),
            text_values = x.get("text_values"), text_pattern=x.get("text_pattern")
        )
        return d
        
    def check(self, value):
        if isinstance(value, int):
            ok = (
                (self.IntValues is None or value in self.IntValues) \
                and (self.IntMin is None or value >= self.IntMin) \
                and (self.IntMax is None or value <= self.IntMax) 
            )
            if not ok:  return False
            value = float(value)        # check floating point constraints too
        
        if isinstance(value, float):
            ok = (
                (self.FloatValues is None or value in self.FloatValues) \
                and (self.FloatMin is None or value >= self.FloatMin) \
                and (self.FloatMax is None or value <= self.FloatMax) 
            )
            if not ok:  return False
        
        if isinstance(value, str):
            ok = (
                (self.TextPattern is None or self.TextPattern.match(value) is not None) \
                and (self.TextValues is None or value in self.TextValues)
            )
            
        return ok
        
class DBParamCategory(object):

    def __init__(self, db, path, owner):
        self.Path = path
        self.DB = db
        if isinstance(owner, str):
            owner = DBRole.get(db, owner)
        self.Owner = owner
        self.Restricted = False
        self.Definitions = None           # relpath -> definition
        
    def save(self, do_commit=True):
        c = self.DB.cursor()
        defs = {name:d.to_jsonable() for name, d in self.Definitions.items()}
        defs = json.dumps(defs)
        c.execute("""
            insert into parameter_categories(path, owner, restricted, definitions) values(%{path}s, %{owner}s, %{restricted}s, %{defs}s)
                on conflict(path) 
                    do update set owner=%{owner}s, restricted=%{restricted}s, definitions=%{defs}s
            """,
            dict(path=self.Path, owner=self.Owner.Name, restricted=self.Restricted, defs=defs))
        if do_commit:
            c.execute("commit")
        return self
    
    @staticmethod
    def get(db, path):
        c = db.cursor()
        c.execute("""
            select owner, restricted, definitions from parameter_categories where path=%s""", (path)
        )
        tup = c.fetchone()
        if not tup:
            return None
        owner, restricted, defs = tup
        defs = defs or {}
        cat = DBParamCategory(db, path, owner)
        cat.Restricted = restricted
        cat.Definitions = {name: DBParamDefinition.from_json(d) for name, d in defs.items()}
        return cat
        
    @staticmethod
    def exists(db, path):
        return DBParamCategory.get(db, path) != None
        
    @staticmethod
    def category_for_path(db, path):
        # get the deepest category containing the path
        words = path.split(".")
        paths = ["."]
        p = []
        for w in words:
            p.append(w)
            paths.append(".".join(p))
            
        c = db.cursor()
        c.execute("""
            select path, owner, restricted from parameter_categories where path in %s
                order by path desc limit 1""", (paths,)
        )
        
        tup = c.fetchone()
        cat = None
        if tup:
            path, owner, restricted = tup
            cat = DBParamCategory(db, path, owner)
            cat.Restricted = restricted
        return cat
        
    def check_metadata(self, name, value):
        # name is relative to the category path
        defs = self.definitions
        d = defs.get(name)
        if d is not None:
            if not d.check(v):
                raise ValueError(f"Invalid value for {name}: {v}")
        elif self.Restricted:
            raise ValueError(f"Unknown name {name} in a restricted category")

class DBParamValidator(object):
    
    def __init__(self, db):
        self.DB = db
        self.Categories = {}        # param parent path -> category. Category can be None !
        
    def validate_metadata(self, meta):
        for k, v in sorted(meta.items()):
            words = k.rsplit(".", 1)
            if len(words) != 2:
                parent = ""
                name = k
            else:
                parent, name = words                
            cat = self.Categories.get(parent, -1)
            if cat == -1:
                self.Categories[parent] = cat = DBParamCategory.category_for_path(self.DB, parent)
            if cat is not None:
                cat.check_metadata(name, v)

                
        
    
