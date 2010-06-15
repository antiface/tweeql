from itertools import chain
from ssql import operators
from ssql.aggregation import get_aggregate_factory
from ssql.operators import StatusSource
from ssql.exceptions import QueryException
from ssql.field_descriptor import FieldDescriptor
from ssql.field_descriptor import FieldType
from ssql.function_registry import FunctionRegistry
from ssql.query import Query
from ssql.query import QueryTokens
from ssql.ssql_parser import gen_parser
from ssql.tuple_descriptor import TupleDescriptor
from ssql.twitter_fields import twitter_tuple_descriptor

def gen_query_builder():
    return QueryBuilder()

class QueryBuilder:
    """
        Generates a query from a declarative specification in a SQL-like syntax.
        This class is not thread-safe
    """
    def __init__(self):
        self.parser = gen_parser()
        self.function_registry = FunctionRegistry()
        self.unnamed_operator_counter = 0
        self.twitter_td = twitter_tuple_descriptor()
    def build(self, query_str):
        """
            Takes a Unicode string query_str, and outputs a query tree
        """
        parsed = self.parser.parseString(query_str)
        source = self.__get_source(parsed)
        tree = self.__get_tree(parsed)
        query = Query(tree, source)
        return query
    def __get_source(self, parsed):
        source = parsed.sources[0]
        if source == QueryTokens.TWITTER:
            return StatusSource.TWITTER_FILTER
        elif source.startswith(QueryTokens.TWITTER_SAMPLE):
            return StatusSource.TWITTER_SAMPLE
        else:
            raise QueryException('Unknown query source: %s' % (source))
    def __get_tree(self, parsed):
        select = parsed.select.asList()[1:][0]
        where_clause = parsed.where.asList()
        groupby = parsed.groupby.asList()
        window = parsed.window.asList()
        window = None if window == [''] else window[1:]
        (tree, where_fields) = self.__parse_where(where_clause)
        tree = self.__add_select_and_aggregate(select, groupby, where_fields, window, tree)
        return tree
    def __parse_where(self, where_clause):
        tree = None
        where_fields = []
        if where_clause == ['']: # no where predicates
            tree = operators.AllowAll() 
        else:
            tree = self.__parse_clauses(where_clause[0][1:], where_fields)
        return (tree, where_fields)
    def __parse_clauses(self, clauses, where_fields):
        """
            Parses the WHERE clauses in the query.  Adds any fields it discovers to where_fields
        """
        self.__clean_list(clauses)
        if type(clauses) != list: # This is a token, not an expression 
            return clauses
        elif clauses[0] == QueryTokens.WHERE_CONDITION: # This is an operator expression
            return self.__parse_operator(clauses[1:], where_fields)
        else: # This is a combination of expressions w/ AND/OR
            # ands take precedent over ors, so 
            # A and B or C and D -> (A and B) or (C and D)
            ands = []
            ors = []
            i = 0
            while i < len(clauses):
                ands.append(self.__parse_clauses(clauses[i], where_fields))
                if i+1 == len(clauses):
                    ors.append(self.__and_or_single(ands))
                else:
                    if clauses[i+1] == QueryTokens.OR:
                        ors.append(self.__and_or_single(ands))
                        ands = []
                    elif clauses[i+1] == QueryTokens.AND:
                        pass
                i += 2
            # TODO: rewrite __and_or_single to handle the ors below just
            # like it does the ands above 
            if len(ors) == 1:
                return ors[0]
            else:
                return operators.Or(ors)
    def __parse_operator(self, clause, where_fields):
        if len(clause) == 3 and clause[1] == QueryTokens.CONTAINS:
            alias = self.__where_field(clause[0], where_fields)
            return operators.Contains(alias, clause[2])
        elif len(clause) == 3 and clause[1] == QueryTokens.EQUALS:
            alias = self.__where_field(clause[0], where_fields)
            return operators.Equals(alias, clause[2])
        elif len(clause) == 3 and clause[1] == QueryTokens.EXCLAIM_EQUALS:
            alias = self.__where_field(clause[0], where_fields)
            return operators.Not(operators.Equals(alias, clause[2]))
    def __where_field(self, field, where_fields):
        (field_descriptors, verify) = self.__parse_field(field, self.twitter_td, False, False)
        alias = field_descriptors[0].alias
        # name the field whatever alias __parse_field gave it so it can be
        # passed to __parse_field in the future and have a consistent name
        if not ((len(field) >= 3) and (field[-2] == QueryTokens.AS)):
            field.append(QueryTokens.AS)
            field.append(alias)
        where_fields.append(field)
        return alias
    def __clean_list(self, list):
        self.__remove_all(list, QueryTokens.LPAREN)
        self.__remove_all(list, QueryTokens.RPAREN)

    def __remove_all(self, list, token):
        while token in list:
            list.remove(token)
    def __and_or_single(self, ands):
        if len(ands) == 1:
            return ands[0]
        else:
            return operators.And(ands)
    def __add_select_and_aggregate(self, select, groupby, where, window, tree):
        """
            select, groupby, and where are a list of unparsed fields
            in those respective clauses
        """
        tuple_descriptor = TupleDescriptor()
        fields_to_verify = []
        all_fields = chain(select, where)
        if groupby != ['']:
            groupby = groupby[1:][0]
            all_fields = chain(all_fields, groupby)
        self.__remove_all(groupby, QueryTokens.EMPTY_STRING)     
        for field in all_fields:
            (field_descriptors, verify) = self.__parse_field(field, self.twitter_td, True, False)
            fields_to_verify.extend(verify)
            tuple_descriptor.add_descriptor_list(field_descriptors)
        for field in fields_to_verify:
            self.__verify_and_fix_field(field, tuple_descriptor)
        
        # at this point, tuple_descriptor should contain a tuple descriptor
        # with fields/aliases that are correct (we would have gotten an
        # exception otherwise.  built select_descriptor/group_descriptor
        # from it
        select_descriptor = TupleDescriptor()
        group_descriptor = TupleDescriptor()
        aggregates = []
        for field in select:
            (field_descriptors, verify) = self.__parse_field(field, tuple_descriptor, True, True)
            select_descriptor.add_descriptor_list(field_descriptors)
            if field_descriptors[0].field_type == FieldType.AGGREGATE:
                aggregates.append(field_descriptors[0])
        # add WHERE clause fields as invisible attributes
        for field in where:
            (field_descriptors, verify) = self.__parse_field(field, tuple_descriptor, True, False)
            select_descriptor.add_descriptor_list(field_descriptors)
        if len(aggregates) > 0:
            if window == None:
                raise QueryException("Aggregate expression provided with no WINDOW parameter")
            for field in groupby:
                (field_descriptors, verify) = self.__parse_field(field, tuple_descriptor, True, True)
                group_descriptor.add_descriptor_list(field_descriptors)
            for alias in select_descriptor.aliases:
                select_field = select_descriptor.get_descriptor(alias)
                group_field = group_descriptor.get_descriptor(alias)
                if group_field == None and \
                   select_field.field_type != FieldType.AGGREGATE and \
                   select_field.visible:
                    raise QueryException("'%s' appears in the SELECT but is is neither an aggregate nor a GROUP BY field" % (alias))
            tree = operators.GroupBy(tree, group_descriptor, aggregates, window)
        tree.assign_descriptor(select_descriptor)
        return tree
    def __parse_field(self, field, tuple_descriptor, alias_on_complex_types, make_visible):
        """
            Returns a tuple containing (field_descriptors, fieldnames_to_verify)

            The first field in field_descriptors is the one requested to be parsed by this
            function call.  If the field turns out to be an aggregate or a user-defined
            function call, then field_descriptors will contain those parsed field descriptors
            as well, with their visible flag set to False.  

            fieldnames_to_verify is a list of field names that should be verified in order
            to ensure that at some point their alias is defined in an AS clause.
        """
        alias = None
        field_type = None
        underlying_fields = None
        aggregate_factory = None
        function = None
        fields_to_verify = []
        parsed_fds = []
        field_backup = list(field)
        self.__clean_list(field)
        
        # parse aliases if they exist
        if (len(field) >= 3) and (field[-2] == QueryTokens.AS):
            alias = field[-1]
            field = field[:-2]

        if len(field) == 1: # field or alias
            if alias == None:
                alias = field[0]
            field_descriptor = tuple_descriptor.get_descriptor(field[0])
            if field_descriptor == None: # underlying field not yet defined.  mark to check later
                field_type = FieldType.UNDEFINED
                underlying_fields = [field[0]]
                # check alias and underlying once this process is done to
                # find yet-undefined fields
                fields_to_verify.append(field[0])
                fields_to_verify.append(alias)
            else: # field found, copy information
                field_type = field_descriptor.field_type
                underlying_fields = field_descriptor.underlying_fields
                aggregate_factory = field_descriptor.aggregate_factory
                function = field_descriptor.function
        elif len(field) > 1: # function or aggregate  
            if alias == None:
                if alias_on_complex_types:
                    raise QueryException("Must specify alias (AS clause) for '%s'" % (repr(field)))
                else:
                    self.unnamed_operator_counter += 1
                    alias = "operand%d" % (self.unnamed_operator_counter)
            underlying_field_list = field[1:]
            underlying_fields = []
            for underlying in underlying_field_list:
                (parsed_fd_list, parsed_verify) = self.__parse_field(underlying, tuple_descriptor, False, False)
                for parsed_fd in parsed_fd_list:
                    parsed_fd.visible = False
                fields_to_verify.extend(parsed_verify)
                parsed_fds.extend(parsed_fd_list)
                underlying_fields.append(parsed_fd_list[0].alias)
            aggregate_factory = get_aggregate_factory(field[0])
            if aggregate_factory != None: # found an aggregate function
                field_type = FieldType.AGGREGATE
            else:
                function = self.function_registry.get_function(field[0])
                if function != None:
                    field_type = FieldType.FUNCTION
                else:
                    raise QueryException("'%s' is neither an aggregate or a registered function" % (field[0]))
        else:
            raise QueryException("Empty field clause found: %s" % ("".join(field_backup)))
        fd = FieldDescriptor(alias, underlying_fields, field_type, aggregate_factory, function)
        fd.visible = make_visible
        parsed_fds.insert(0, fd)
        return (parsed_fds, fields_to_verify)
    
    def __verify_and_fix_field(self, field, tuple_descriptor):
        field_descriptor = tuple_descriptor.get_descriptor(field)
        error = False
        if field_descriptor == None:
            error = True
        elif field_descriptor.field_type == FieldType.UNDEFINED:
            if field == field_descriptor.underlying_fields[0]:
                error = True
            else:
                referenced_field_descriptor = \
                    self.__verify_and_fix_field(field_descriptor.underlying_fields[0],
                                                tuple_descriptor)
                field_descriptor.underlying_fields = referenced_field_descriptor.underlying_fields
                field_descriptor.field_type = referenced_field_descriptor.field_type
                field_descriptor.aggregate_factory = referenced_field_descriptor.aggregate_factory
                field_descriptor.function = referenced_field_descriptor.function
        if error:
            raise QueryException("Field '%s' is neither a builtin field nor an alias" % (field))
        else:
            return field_descriptor