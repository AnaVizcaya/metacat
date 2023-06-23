from metacat.util import fetch_generator
import json

def transactioned(method):
    def decorated(first, *params, transaction=None, **args):

        if transaction is not None:
            return method(first, *params, transaction=transaction, **args)

        if isinstance(first, HasDB):
            transaction = first.DB.transaction()
        elif isinstance(first, type):
            # class method -- DB is second argument
            transaction = params[0].transaction()
        else:
            transaction = first.transaction()       # static method

        with transaction:
            return method(first, *params, transaction=transaction, **args)

    return decorated

def insert_many(transaction, table, column_names, tuples, copy_threshold = 100):

    # if the tuples list or iterable is short enough, do it as multiple inserts
    tuples_lst, tuples = make_list_if_short(tuples, copy_threshold)
    if tuples_lst is not None and len(tuples_lst) <= copy_threshold:
        columns = ",". join(column_names)
        placeholders = ",".join(["%s"]*len(column_names))
        try:
            transaction.executemany(f"""
                insert into parent_child({columns}) values({placeholders})
            """, tuples_lst)
            if do_commit:   cursor.execute("commit")
        except Exception as e:
            cursor.execute("rollback")
            raise
    else:
        
        csv_file = io.StringIO()
        writer = csv.writer(csv_file, delimiter='\t', quoting=csv.QUOTE_MINIMAL)

        for tup in tuples:
            assert len(tup) == len(column_names)
            tup = ["\\N" if x is None else x for x in tup]
            writer.writerow(tup)
        csv_file.seek(0,0)
        try:
            cursor.copy_from(csv_file, table, columns = column_names)
            if do_commit:   cursor.execute("commit")
        except Exception as e:
            cursor.execute("rollback")
            raise
        



class DBObject(object):

    PK = None
    Table = None
    Columns = None

    def __init__(self, db):
        self.DB = db

    @classmethod
    def columns(cls, table_name=None, as_text=True, exclude=[]):
        if isinstance(exclude, str):
            exclude = [exclude]
        clist = [c for c in cls.Columns if c not in exclude]
        if table_name:
            clist = [table_name+"."+cn for cn in clist]
        if as_text:
            return ",".join(clist)
        else:
            return clist

    @classmethod
    def list(cls, db):
        c = db.cursor()
        columns = cls.columns()
        c.execute(f"select {columns} from {cls.Table}")
        return (cls.from_tuple(db, tup) for tup in fetch_generator(c))

    @classmethod
    def get(cls, db, *pkvalues):
        assert len(pkvalues) == len(cls.PK)
        wheres = " and ".join([f"{pkc} = %s" for pkc in cls.PK])
        columns = cls.columns()
        sql = f"""
            select {columns}
                from {cls.Table}
                where {wheres}
        """
        c = db.cursor()
        c.execute(sql, pkvalues)
        tup = c.fetchone()
        return None if tup is None else cls.from_tuple(db, tup)
        
    @classmethod
    def exists(cls, db, *pkvalues):
        return cls.get(db, *pkvalues) is not None
        
    def to_json(self):
        return json.dumps(self.to_jsonable())

    @classmethod
    def from_tuple(cls, db, tup):
        return cls(db, *tup)                # default implementstion

    @classmethod
    def from_tuples(cls, db, tuples):
        for tup in tuples:
            yield cls.from_tuple(db, tup)
        
class DBManyToMany(object):
    
    def __init__(self, db, table, *reference_columns, **lookup_values):
        self.DB = db
        self.Table = table
        self.LookupValues = lookup_values
        self.Where = "where " + " and ".join(["%s = '%s'" % (name, value) for name, value in lookup_values.items()])
        assert len(reference_columns) >= 1
        self.ReferenceColumns = list(reference_columns)
        
    def list(self, c=None):
        columns = ",".join(self.ReferenceColumns) 
        if c is None: c = self.DB.cursor()
        c.execute(f"select {columns} from {self.Table} {self.Where}")
        if len(self.ReferenceColumns) == 1:
            return (x for (x,) in fetch_generator(c))
        else:
            return fetch_generator(c)
        
    def __iter__(self):
        return self.list()
        
    def add(self, *vals, c=None):
        assert len(vals) == len(self.ReferenceColumns)
        col_vals = list(zip(self.ReferenceColumns, vals)) + list(self.LookupValues.items())
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
        assert len(vals) == len(self.ReferenceColumns)
        col_vals = list(zip(self.ReferenceColumns, vals))
        where = self.Where + " and " + " and ".join(["%s='%s'" % (k,v) for k, v in col_vals])
        if c is None: c = self.DB.cursor()
        c.execute(f"""
            select exists(
                    select * from {self.Table} {where} limit 1
            )
        """)
        return c.fetchone()[0]
        
    def __contains__(self, v):
        if not isinstance(v, tuple): v = (v,)
        return self.contains(*v)

    def remove(self, *vals, c=None, all=False):
        assert all or len(vals) == len(self.VarColumns)
        if c is None: c = self.DB.cursor()
        where = self.Where
        if not all:
            col_vals = list(zip(self.ReferenceColumns, vals))
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
        
