import itertools, io, csv, json
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

class DatasetCircularDependencyDetected(Exception):
    pass


class NotFoundError(Exception):
    def __init__(self, msg):
        self.Message = msg

    def __str__(self):
        return "Not found error: %s" % (self.Message,)


def parse_name(name, default_namespace=None):
    words = (name or "").split(":", 1)
    if not words or not words[0]:
        assert not not default_namespace, "Null default namespace"
        ns = default_namespace
        name = words[-1]
    else:
        assert len(words) == 2, "Invalid namespace:name specification:" + name
        ns, name = words
    return ns, name


class MetaValidationError(Exception):
    
    def __init__(self, message, errors):
        self.Errors = errors
        self.Message = message
        
    def as_json(self):
        return json.dumps(
            {
                "message":self.Message,
                "metadata_errors":self.Errors
            }
        )
        
def make_list_if_short(iterable, limit):
    # convert iterable to list if it is short. otherwise return another iterable with the same elements
    
    if isinstance(iterable, (list, tuple)):
        return iterable, None
    
    head = []
    if len(head) < limit:
        for x in iterable:
            head.append(x)
            if len(head) > limit:
                return None, itertools.chain(head, iterable)
        else:
            return head, None
    else:
        return None, iterable

def insert_bulk(cursor, table, column_names, tuples, do_commit=True, copy_threshold = 100):

    # if the tuples list or iterable is short enough, do it as multiple inserts
    tuples_lst, tuples = make_list_if_short(tuples, copy_threshold)
    if tuples_lst is not None and len(tuples_lst) <= copy_threshold:
        columns = ",". join(column_names)
        placeholders = ",".join(["%s"]*len(column_names))
        try:
            cursor.executemany(f"""
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
        
