from collections import defaultdict
from semanticsimilarity import HpoEnsmallen
from scipy.stats import chi2_contingency, fisher_exact
import pyspark
from warnings import warn
import pandas as pd
import math


class HpoClusterAnalyzer:
    """A class for analyzing clusters for HPO terms that are distributed unevenly in
    the clusters
    """

    def __init__(self, hpo: HpoEnsmallen):
        """Constructor

        :param hpo: an HpoEnsmallen object representing the HPO graph
        """
        self._percluster_termcounts = defaultdict(dict)
        self._per_cluster_total_pt_count = defaultdict(int)
        self._hpo_terms = set()
        if not isinstance(hpo, HpoEnsmallen):
            raise ValueError("hpo argument must be an object of type HpoEnsmallen")
        self._hpo = hpo
        self._total_patients = 0
        self._clusters = []

    def add_counts(self,
                   patient_hpo_df,
                   cluster_assignment_df,
                   ph_patient_id_col='patient_id',
                   ph_hpo_col='hpo_id',
                   ca_patient_id_col='patient_id',
                   ca_cluster_col='cluster'):
        """

        :param patient_hpo_df: Spark DF with two columns: patient ID and HPO term
        :param cluster_assignment_df: Spark DF with two columns: patient ID and cluster assignment
        :param ph_patient_id_col: column in patient_hpo_df with patient ID [patient_id]
        :param ph_hpo_col: column in patient_hpo_df with HPO id [hpo_id]
        :param ca_patient_id_col: column in cluster_assignment_df with patient ID [patient id]
        :param ca_cluster_col: column in cluster_assignment_df with cluster assignment [cluster]
        :return: None
        """
        cluster_dict = {row[ca_patient_id_col]: row[ca_cluster_col] for row in cluster_assignment_df.collect()}

        patient_ids = [row[ca_patient_id_col] for row in cluster_assignment_df.collect()]
        if len(patient_ids) != len(set(patient_ids)):
            raise ValueError(f"column {ca_patient_id_col} in cluster_assignment has duplicate rows for some patients!")

        self._unique_pt_ids = list(set([row[ca_patient_id_col] for row in cluster_assignment_df.collect()]))
        self._clusters = list(set(cluster_dict.values()))

        if not isinstance(cluster_assignment_df, pyspark.sql.dataframe.DataFrame):
            raise ValueError("cluster_assignment argument must be Spark DataFrame")
        if ca_patient_id_col not in cluster_assignment_df.columns:
            raise ValueError(f"cluster_assignment argument doesn't have {ca_patient_id_col} column")
        if ca_cluster_col not in cluster_assignment_df.columns:
            raise ValueError(f"cluster_assignment argument doesn't have {ca_cluster_col} column")
        if not isinstance(patient_hpo_df, pyspark.sql.dataframe.DataFrame):
            raise ValueError("counts_df must be a Spark DataFrame")

        # the dataframe must have exactly two columns, the first of
        # which is called patient_id and the second is called hpo_id
        if ph_patient_id_col not in patient_hpo_df.columns or ph_hpo_col not in patient_hpo_df.columns:
            raise ValueError("Columns must be patient_id and hpo_id, but we got {}".format(";".join(patient_hpo_df.columns)))

        # Group by patient_id and create a dataframe with one row per patient_id
        # as well as a list of the hpo_id's for that patient id
        df_by_patient_id = patient_hpo_df.toPandas().groupby(ph_patient_id_col)[ph_hpo_col].apply(list)
        print(df_by_patient_id)
        # now we can create a set that contains all of the ancestors of all terms
        # to which each patient is annotated and then we use this to increment
        # the counts dictionary.
        # df_by_patient_id is a pandas series, whence iteritems()
        for patient_id, row in df_by_patient_id.iteritems():
            if patient_id not in cluster_dict:
                raise ValueError(f"Could not find patient {patient_id} in cluster_assignment")
            cluster = cluster_dict.get(patient_id)
            self._per_cluster_total_pt_count[cluster] += 1
            if cluster not in self._percluster_termcounts:
                self._percluster_termcounts[cluster] = defaultdict(int)
            # pat_id = row[0]
            hpo_id_list = row
            # ####### TODO #################
            # remove the following before production, but now
            # we still need to check sanity
            if not isinstance(hpo_id_list, list):
                raise ValueError('hpo_id not list')
            induced_ancestor_graph = set()
            for hpo_id in hpo_id_list:
                self._hpo_terms.add(hpo_id)
                if self._hpo.node_exists(hpo_id):
                    induced_ancestor_graph.add(hpo_id)  # add orignal term, which does not appear in ancestors
                    ancs = self._hpo.get_ancestors(hpo_id)
                    induced_ancestor_graph.update(ancs)
                else:
                    warn(f"Couldn't find {hpo_id} in self._hpo graph")
            self._total_patients += 1
            for hpo_id in induced_ancestor_graph:
                self._percluster_termcounts[cluster][hpo_id] += 1

    def do_chi2(self):
        """Perform chi2 test for each term

        :return: Pandas dataframe with results of chi squared tests
        """

        results_list = []
        for hpo_id in self._hpo_terms:
            with_hpo_count = []
            without_hpo_count = []
            d = {'hpo_id': hpo_id}
            for cluster in self._clusters:
                cluster_with = f"{cluster}-with"
                cluster_without = f"{cluster}-without"
                cluster_total = f"{cluster}-total"
                total = self._per_cluster_total_pt_count[cluster]
                d[cluster_total] = total
                with_hpo = self._percluster_termcounts[cluster][hpo_id]
                d[cluster_with] =  with_hpo
                without_hpo = total - with_hpo
                with_hpo_count.append(with_hpo)
                without_hpo_count.append(without_hpo)
                d[cluster_without] = without_hpo
            table = [with_hpo_count, without_hpo_count]

            stat, p, dof, expected = [float('nan') for i in range(4)]
            if not any(without_hpo_count):
                warn(f"hpo_id {hpo_id} is in all clusters - can't compute chi-2")
            elif not any(with_hpo_count):
                warn(f"hpo_id {hpo_id} doesn't occur in any clusters - can't compute chi-2")
            else:
                stat, p, dof, expected = chi2_contingency(table)
            d['stat'] = stat
            d['p'] = p
            d['dof'] = dof
            d['expected'] = expected
            results_list.append(d)
        return pd.DataFrame(results_list)

    def do_fisher_exact(self):
        """Perform chi2 test for each term

        :return: Pandas dataframe with results of Fisher exact tests
        """
        results_list = []
        for hpo_id in self._hpo_terms:
            with_hpo_count = []
            without_hpo_count = []
            d = {'hpo_id': hpo_id}
            for cluster in self._clusters:
                cluster_with = f"{cluster}-with"
                cluster_without = f"{cluster}-without"
                cluster_total = f"{cluster}-total"
                total = self._per_cluster_total_pt_count[cluster]
                d[cluster_total] = total
                with_hpo = self._percluster_termcounts[cluster][hpo_id]
                d[cluster_with] = with_hpo
                without_hpo = total - with_hpo
                with_hpo_count.append(with_hpo)
                without_hpo_count.append(without_hpo)
                d[cluster_without] = without_hpo

            oddsr, p = fisher_exact([with_hpo_count, without_hpo_count])
            d['oddsr'] = oddsr
            d['p'] = p
            results_list.append(d)

        return pd.DataFrame(results_list)

    @staticmethod
    def do_chi_square_on_covariates(covariate_dataframe: pd.DataFrame,
                                    cluster_col: str = 'cluster',
                                    minimum_n: int = 5,
                                    ignore_col: list = [],
                                    bonferroni: bool = True) -> pd.DataFrame:
        """A static method for performing chi square on covariates for which we have cluster info.

        covariate_dataframe should be a Pandas dataframe with a column describing cluster
        information, and other boolean or factor data, like so:

        .. list-table::
           :widths: 25 25 50
           :header-rows: 1

           * - cluster
             - diabetes
             - gender
           * - 1
             - True
             - Male
           * - 2
             - True
             - Female
           * - 3
             - False
             - Unknown
           * - 2
             - False
             - Female
           * - 1
             - False
             - Male

        This method return a pandas dataframe with these statistics about rows that were
        analyzed:

        .. list-table::
           :widths: 25 25 50 50 10
           :header-rows: 1

           * -
             - covariate
             - chi2
             - p
             - dof
           * - 0
             - diabetes
             - 100.000000
             - 1.554159e-21
             - 3
           * - 1
             - gender
             - 123.000000
             - 2.3e-1
             - 1

        :param covariate_dataframe: pd.DataFrame
        :param cluster_col: str [default 'cluster'] the column containing cluster information for each person
        :param minimum_n: minimal number of values necessary to calculate chisq - otherwise stats are set to float('NaN')
        :param ignore_col: ignore these covariate columns - don't calculate stats on them.
        :param bonferroni: do a bonferroni correction (p value * number of tests)
        :return: pd.DataFrame

        """
        if cluster_col not in covariate_dataframe.columns:
            raise ValueError(f"cluster_col arg {cluster_col} is not a column in covariate_dataframe")

        results = []
        for column in covariate_dataframe.columns:
            if column == cluster_col or column in ignore_col:
                continue
            contingency_table = pd.crosstab(covariate_dataframe[cluster_col], covariate_dataframe[column])
            if covariate_dataframe[column].dtype == bool and len(covariate_dataframe[column][covariate_dataframe[column]]) < minimum_n:
                chi2, p_value, dof, exp = float('NaN'), float('NaN'), float('NaN'), float('NaN')
            else:
                chi2, p_value, dof, exp = chi2_contingency(contingency_table)
            d = {'covariate': column, 'chi2': chi2, 'p': p_value, 'dof': dof}

            d, factor_to_append = add_true_counts_by_cluster(d, contingency_table, covariate_dataframe[column].dtype)
            results.append(d)

        results_pd = pd.DataFrame(results)
        if bonferroni:
            results_pd['p'] = results_pd['p'].apply(lambda x: x*results_pd.shape[0])
            results_pd['p'] = results_pd['p'].apply(lambda x: x if x < 1 or math.isnan(x) else 1)  # no p-values > 1

        return results_pd


def add_true_counts_by_cluster(d: dict, contingency_table: pd.DataFrame, column_dtype: str):
    """Add true counts by cluster

    :param d: a dict with counts
    :param contingency_table: a Pandas dataframe with contingency table
    :param column_dtype: dtype of col [str]
    :return:
    """
    if contingency_table.shape[1] <= 2:
        if column_dtype == bool:
            true_counts_col = True
        elif '1' in contingency_table.columns:
            true_counts_col = '1'
        elif 1 in contingency_table.columns:
            true_counts_col = 1
        else:  # just take the first one if this isn't a boolean thing
            true_counts_col = contingency_table.columns[0]
        true_counts_by_cluster = list(contingency_table[true_counts_col])
    else:
        # otherwise set everything to NaN - too confusing to report counts for covariates more than 2 factors
        true_counts_col = None
        true_counts_by_cluster = [float('NaN')] * contingency_table.shape[0]

    total_counts_by_cluster = contingency_table.sum(axis=1)

    # add cluster true counts info to existing dict
    keys = ['cluster' + str(contingency_table.index[idx]) for idx in range(contingency_table.shape[0])]
    new_d = dict(zip(keys, true_counts_by_cluster))
    d = {**d, **new_d}

    # add cluster total counts info to existing dict
    keys = ['cluster' + str(contingency_table.index[idx]) + "_total" for idx in range(contingency_table.shape[0])]
    new_d_total = dict(zip(keys, total_counts_by_cluster))
    d = {**d, **new_d_total}

    return d, true_counts_col
